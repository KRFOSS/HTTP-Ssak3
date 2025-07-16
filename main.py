import os
import subprocess
import aiohttp
from bs4 import BeautifulSoup, SoupStrainer
from urllib.parse import urljoin, urlparse
import sys
import orjson
import asyncio
import aiofiles
from datetime import datetime
import typer
from typing import List, Optional


app = typer.Typer()


async def load_visited_urls(visited_urls_file: str):
    if os.path.exists(visited_urls_file):
        try:
            async with aiofiles.open(visited_urls_file, "r", encoding="utf-8") as f:
                content = await f.read()
                data = orjson.loads(content)
                visited_urls = set(data.get("visited_urls", []))
                print(f"📁 이전 방문 기록을 로드했습니다: {len(visited_urls)}개 URL")
                return visited_urls
        except (orjson.JSONDecodeError, IOError) as e:
            print(f"⚠️ 방문 기록 파일을 읽는 중 오류 발생: {e}")
            print("   새로운 방문 기록을 시작합니다.")

    return set()


async def save_visited_urls(visited_urls: set, visited_urls_file: str):
    try:
        data = {
            "visited_urls": list(visited_urls),
            "last_updated": datetime.now().isoformat(),
        }
        async with aiofiles.open(visited_urls_file, "wb") as f:
            await f.write(
                orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)
            )
    except IOError as e:
        print(f"⚠️ 방문 기록 저장 중 오류 발생: {e}")


def check_aria2c_installed():
    try:
        subprocess.run(["aria2c", "--version"], check=True, capture_output=True)
        print("✅ 'aria2c'가 설치되어 있습니다.")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ 오류: 'aria2c'가 설치되어 있지 않거나 PATH에 없습니다.")
        print("이 스크립트를 실행하려면 aria2를 설치해주세요.")
        print("  - Debian/Ubuntu: sudo apt-get install aria2")
        print("  - CentOS/RHEL: sudo yum install aria2")
        return False


async def download_file_with_aria2(
    file_url: str,
    local_dir: str,
    aria2_options: List[str],
    error_urls_file: str,
):
    print(f"📥 파일 다운로드 시도: {file_url}")
    if file_url.endswith(".metalink"):
        return await download_file_with_aiohttp(file_url, local_dir, error_urls_file)
    command = ["aria2c", "-c", "--dir", local_dir, *aria2_options, file_url]

    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        stdout_text = stdout.decode("utf-8", errors="ignore") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="ignore") if stderr else ""

        if process.returncode == 0:
            if "download completed" in stdout_text.lower():
                print(f"✅ 다운로드 완료: {file_url}")
                return True
            elif "already been completed" in stdout_text.lower():
                print(f"👍 건너뛰기 (이미 완료됨): {os.path.basename(file_url)}")
                return True
            else:
                print(stdout_text)
                return True
        else:
            print(f"⚠️ aria2c 실행 중 오류 발생: {file_url}")
            print(f"   오류 메시지: {stderr_text}")
            await save_error_url(file_url, error_urls_file)
            return False
    except Exception as e:
        print(f"🚨 예외 발생: {e}")
        await save_error_url(file_url, error_urls_file)
        return False


async def download_file_with_aiohttp(
    file_url: str, local_dir: str, error_urls_file: str
):
    os.makedirs(local_dir, exist_ok=True)
    local_filename = os.path.join(local_dir, os.path.basename(file_url))
    try:
        timeout = aiohttp.ClientTimeout(total=60.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(file_url) as response:
                response.raise_for_status()
                async with aiofiles.open(local_filename, "wb") as f:
                    async for chunk in response.content.iter_chunked(8192):
                        await f.write(chunk)
        print(f"✅ .metalink 파일 다운로드 완료: {local_filename}")
        return True
    except Exception as e:
        print(f"❌ .metalink 파일 다운로드 실패: {file_url}\n   오류: {e}")
        await save_error_url(file_url, error_urls_file)
        return False


async def save_error_url(error_url: str, error_urls_file: str):
    urls = []
    if os.path.exists(error_urls_file):
        try:
            async with aiofiles.open(error_urls_file, "r", encoding="utf-8") as f:
                content = await f.read()
                data = orjson.loads(content)
                urls = data.get("error_urls", [])
        except Exception:
            pass
    if error_url not in urls:
        urls.append(error_url)
    data = {
        "error_urls": urls,
        "last_updated": datetime.now().isoformat(),
    }
    try:
        async with aiofiles.open(error_urls_file, "wb") as f:
            await f.write(
                orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)
            )
    except Exception as e:
        print(f"⚠️ 오류 URL 저장 중 문제 발생: {e}")


async def sync_mirror(
    current_url: str,
    local_path: str,
    visited_urls: set,
    base_sync_url: str,
    aria2_options: List[str],
    visited_urls_file: str,
    error_urls_file: str,
    visited_directories: Optional[set] = None,
):
    if visited_directories is None:
        visited_directories = set()

    normalized_url = current_url.rstrip("/")

    if normalized_url in visited_directories:
        print(f"↪️ 이미 방문한 디렉토리입니다. 건너뜁니다: {current_url}")
        return

    visited_directories.add(normalized_url)

    if normalized_url in visited_urls:
        print(f"↪️ 이미 방문한 URL입니다. 건너뜁니다: {current_url}")
        return

    print(f"\n📂 디렉토리 탐색 중: {current_url}")

    os.makedirs(local_path, exist_ok=True)

    try:
        timeout = aiohttp.ClientTimeout(total=30.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(current_url) as response:
                response.raise_for_status()
                response_text = await response.text()
    except aiohttp.ClientError as e:
        print(f"❌ URL에 접근할 수 없습니다: {current_url}\n   오류: {e}")
        return
    except Exception as e:
        print(f"❌ HTTP 오류 발생: {current_url}\n   오류: {e}")
        return

    strainer = SoupStrainer("a")
    soup = BeautifulSoup(response_text, "lxml", parse_only=strainer)

    links = soup.find_all("a")

    if not links:
        print(f"   -> 해당 디렉토리에서 링크를 찾을 수 없습니다: {current_url}")
        return

    for link in links:
        href = link.get("href")

        if (
            not href
            or href.startswith("?")
            or href.startswith("../")
            or urlparse(href).scheme
        ):
            continue

        next_url = urljoin(current_url, href)

        if not next_url.startswith(base_sync_url):
            print(f"↪️ 범위를 벗어난 URL입니다. 건너뜁니다: {next_url}")
            continue

        if next_url == current_url:
            continue

        if href.endswith("/"):
            normalized_next_url = next_url.rstrip("/")
            if normalized_next_url not in visited_directories:
                next_local_path = os.path.join(local_path, href.strip("/"))
                await sync_mirror(
                    next_url,
                    next_local_path,
                    visited_urls,
                    base_sync_url,
                    aria2_options,
                    visited_urls_file,
                    error_urls_file,
                    visited_directories,
                )
            else:
                print(f"↪️ 이미 방문한 디렉토리입니다. 건너뜁니다: {next_url}")
        else:
            if next_url not in visited_urls:
                result = await download_file_with_aria2(
                    next_url, local_path, aria2_options, error_urls_file
                )
                if result:
                    visited_urls.add(next_url)
                    await save_visited_urls(visited_urls, visited_urls_file)
            else:
                print(f"↪️ 이미 다운로드한 파일입니다. 건너뜁니다: {next_url}")


@app.command()
def main(
    base_url: str = typer.Option(
        "https://old-releases.ubuntu.com/releases/",
        "--base-url",
        "-u",
        help="다운로드할 HTTP 파일서버의 URL /를 맨뒤에 붙여주세요.\n URL의 경로가 탐색하는 root 지점이 됩니다.",
    ),
    local_download_root: str = typer.Option(
        "/mnt/SSD-1TB/Mirror/ubuntu-old-releases",
        "--local-dir",
        "-d",
        help="파일이 저장될 로컬 디렉토리의 주소",
    ),
    visited_urls_file: str = typer.Option(
        "./visited_urls.json",
        "--visited-log",
        help="방문한 파일 경로의 URL을 저장할 로그파일의 주소",
    ),
    error_urls_file: str = typer.Option(
        "./error_urls.json",
        "--error-log",
        help="오류가 발생한 URL을 저장할 로그파일의 주소",
    ),
    aria2_options: Optional[List[str]] = typer.Option(
        ["-x", "16", "-s", "16", "-k", "1M", "--follow-torrent=false"],
        "--aria2-opts",
        help="aria2c에 전달할 추가 옵션. 예: --aria2-opts -x --aria2-opts 10",
    ),
):
    asyncio.run(
        async_main(
            base_url,
            local_download_root,
            visited_urls_file,
            error_urls_file,
            aria2_options,
        )
    )


async def async_main(
    base_url: str,
    local_download_root: str,
    visited_urls_file: str,
    error_urls_file: str,
    aria2_options: Optional[List[str]],
):
    print("==============================================")
    print("  HTTP-Ssak3 (with aria2)  ")
    print("==============================================")
    print(f"서버 URL: {base_url}")
    print(f"저장 경로: {local_download_root}")
    print(f"방문 기록 파일: {visited_urls_file}")
    print("----------------------------------------------")

    if not check_aria2c_installed():
        sys.exit(1)

    visited_urls = await load_visited_urls(visited_urls_file)

    target_base_url = base_url
    if not target_base_url.endswith("/"):
        target_base_url += "/"

    try:
        await sync_mirror(
            target_base_url,
            local_download_root,
            visited_urls,
            target_base_url,
            aria2_options,
            visited_urls_file,
            error_urls_file,
        )
        print("\n🎉 모든 작업이 완료되었습니다.")
    except KeyboardInterrupt:
        print("\n🚫 사용자에 의해 작업이 중단되었습니다.")
        print("   방문 기록을 저장하는 중...")
        await save_visited_urls(visited_urls, visited_urls_file)
        print("   다음에 다시 실행하면 중단된 지점부터 이어받습니다.")
        sys.exit(0)
    except Exception as e:
        print(f"\n🚨 스크립트 실행 중 예상치 못한 오류가 발생했습니다: {e}")
        await save_visited_urls(visited_urls, visited_urls_file)
        sys.exit(1)
    finally:
        await save_visited_urls(visited_urls, visited_urls_file)
        print(f"💾 방문 기록이 저장되었습니다: {len(visited_urls)}개 URL")


if __name__ == "__main__":
    app()
