#!/usr/bin/env python3
import gradescope_course_manager as gcm
import shutil
import tarfile
import zipfile
import requests
import re
import subprocess
from playwright.sync_api import sync_playwright, Page
from pathlib import Path
from datetime import datetime
import json
import time
import os

CONFIG = {
    'output_dir': 'gradescope_archive',
    'auth_file': 'gradescope_auth.json',
    'delay': 2,
    'headless': False,
    'max_retries': 3,
    'update_threshold_hours': 24,
    'DEFAULT_REPO_PRIVATE': True
}

def setup_auth():
    """Manual login + save session"""
    print("Setting up authentication...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto("https://www.gradescope.com/")
            print("Please log in to Gradescope in the browser window, including any 2FA.")
            print("Once you see your course dashboard, you can close the browser window.")
            page.wait_for_function("() => false", timeout=0)
        except Exception:
            print("\nBrowser closed. Assuming login was successful.")
        
        context.storage_state(path=CONFIG['auth_file'])
        print(f"Authentication session saved to {CONFIG['auth_file']}.")

def get_courses(page: Page) -> list:
    """Return list of course dicts with parsed name components, filtering out ignored courses."""
    print("Discovering courses...")

    # Load ignored courses from ignore_courses.json
    ignore_patterns = []
    ignore_file = Path("ignore_courses.json")
    if ignore_file.exists():
        with open(ignore_file, 'r') as f:
            try:
                ignore_patterns = json.load(f)
                if isinstance(ignore_patterns, list):
                    print(f"  ✓ Loaded {len(ignore_patterns)} ignore patterns.")
                else:
                    print("  ✗ Warning: ignore_courses.json should contain a JSON list of strings.")
                    ignore_patterns = []
            except json.JSONDecodeError:
                print("  ✗ Warning: Could not parse ignore_courses.json.")
    
    # Navigate to base URL and try to click the "Back to Home" link
    page.goto('https://www.gradescope.com/')
    page.wait_for_load_state('networkidle')
    try:
        page.get_by_role("link", name="Gradescope: Back to Home").click()
        page.wait_for_load_state('networkidle')
    except Exception:
        # Fallback to a direct courses page navigation if the link isn't there
        page.goto('https://www.gradescope.com/courses')
        page.wait_for_load_state('networkidle')
    
    # Click "See older courses" until no more courses to load
    while True:
        try:
            older_button = page.get_by_role("button", name="See older courses")
            if older_button.is_visible(timeout=1000):
                older_button.click()
                page.wait_for_load_state('networkidle', timeout=5000)
                time.sleep(CONFIG['delay'])
            else:
                break
        except Exception:
            break
    
    all_discovered_courses = []
    seen_urls = set()
    
    # Use the correct selector for Gradescope course cards
    for card in page.locator("a.courseBox").all():
        try:
            href = card.get_attribute('href')
            if not href or "/courses/" not in href:
                continue
            
            # Skip assignment/submission links
            if any(k in href for k in ["/assignments/", "/submissions/"]):
                continue
            
            # Build full URL
            url = f"https://www.gradescope.com{href}" if href.startswith('/') else href
            
            # Skip duplicates
            if url in seen_urls:
                continue
            seen_urls.add(url)
            
            # Extract course details with fallbacks
            full_name = card.locator(".courseBox--name").text_content().strip() if card.locator(".courseBox--name").count() > 0 else "Unknown"
            short_name = card.locator(".courseBox--shortname").text_content().strip() if card.locator(".courseBox--shortname").count() > 0 else full_name
            term = card.locator(".courseBox--term").text_content().strip() if card.locator(".courseBox--term").count() > 0 else ""
            
            all_discovered_courses.append({
                'url': url,
                'full_name': full_name,
                'short_name': short_name,
                'term': term
            })
            
        except Exception as e:
            # Log but don't fail on individual course extraction errors
            print(f"    Warning: Failed to extract course info: {e}")
            continue

    # Filter out ignored courses
    filtered_courses = []
    for course in all_discovered_courses:
        is_ignored = False
        for pattern in ignore_patterns:
            if pattern in course['full_name']:
                print(f"  - Ignoring course '{course['full_name']}' due to pattern: '{pattern}'")
                is_ignored = True
                break
        if not is_ignored:
            filtered_courses.append(course)
    
    print(f"Found {len(filtered_courses)} courses after filtering.")
    return filtered_courses

def download_assignment(page: Page, assignment_name: str, assignment_url: str, assignment_dir: Path):
    """Downloads files for an assignment, attempting all available downloads."""
    print(f"  -> Processing assignment: {assignment_name}")
    page.goto(assignment_url)
    page.wait_for_load_state('networkidle')
    
    assignment_dir.mkdir(parents=True, exist_ok=True)
    
    # Attempt all direct downloads (archives, code files, PDFs)
    overall_download_count = _try_direct_downloads(page, assignment_name, assignment_dir)

    if overall_download_count > 0:
        print(f"    ✓ Downloaded {overall_download_count} file(s) for '{assignment_name}'.")
    else:
        print(f"    ✗ No files could be downloaded for '{assignment_name}'.")
    
    time.sleep(CONFIG['delay'])


def _download_file_with_requests(page: Page, url: str, assignment_dir: Path) -> bool:
    """Downloads a file from a given URL using requests."""
    try:
        cookies = {c['name']: c['value'] for c in page.context.cookies()}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'
        }
        
        print(f"    Downloading via requests from: {url[:60]}...")
        response = requests.get(url, cookies=cookies, headers=headers, allow_redirects=True, timeout=20)
        response.raise_for_status()

        filename = "downloaded_file"
        if 'content-disposition' in response.headers:
            d = response.headers['content-disposition']
            found_filenames = re.findall('filename="?([^"]+)"?', d)
            if found_filenames:
                filename = found_filenames[0]
        else:
            parsed_url_path = Path(requests.utils.urlparse(url).path)
            if parsed_url_path.name:
                filename = parsed_url_path.name
        
        filename = "".join(c for c in filename if c.isalnum() or c in '._- ').strip()
        filepath = assignment_dir / filename

        filepath.write_bytes(response.content)
        
        print(f"      ✓ Downloaded: '{filename}'")
        
        # Extract archive if applicable
        _extract_if_archive(filepath, assignment_dir)
        
        return True
    except Exception as e:
        print(f"      ✗ Download failed. Details: {str(e)[:100]}")
        return False

def _try_direct_downloads(page: Page, assignment_name: str, assignment_dir: Path) -> int:
    """
    Attempt to download all available files directly using a generalized requests-based method.
    """
    print("    Looking for direct download links (using requests-based method)...")
    
    direct_download_selectors = [
        'a[href*="/download_submission"]',
        'a[download]',
        'a[href$=".zip"]',
        'a[href$=".tar.gz"]',
        'a[href$=".tar"]',
        'a[href$=".tgz"]',
        'a[href$=".py"]',
        'a[href$=".java"]',
        'a[href$=".cpp"]',
        'a[href$=".c"]',
        'a[href$=".h"]',
        'a[href$=".txt"]',
        'a[href$=".pdf"]',
        'a:has-text("Download Graded Copy")',
    ]
    
    successful_downloads = 0
    downloaded_urls = set()

    for selector in direct_download_selectors:
        links = page.locator(selector).all()
        
        for i, link in enumerate(links):
            try:
                href = link.get_attribute('href')
                if not href or href in downloaded_urls:
                    continue

                url = f"https://www.gradescope.com{href}" if href.startswith('/') else href
                downloaded_urls.add(href)
                
                if _download_file_with_requests(page, url, assignment_dir):
                    successful_downloads += 1
                
            except Exception as e:
                print(f"      ✗ Failed to process link (selector: '{selector}'). Details: {str(e)[:100]}")
                continue
    
    return successful_downloads
def _extract_if_archive(filepath: Path, extract_to: Path, depth=0):
    """Recursively extract archives up to one level deep."""
    if depth > 1:
        return

    ext = _get_full_extension(filepath)
    
    if ext not in ['.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2']:
        return

    print(f"      - Detected archive: '{filepath.name}'. Extracting...")
    
    try:
        extracted_files = []
        if ext == '.zip':
            with zipfile.ZipFile(filepath, 'r') as zf:
                zf.extractall(extract_to)
                extracted_files = [extract_to / name for name in zf.namelist()]
        else:  # Various tar formats
            with tarfile.open(filepath, 'r:*') as tf:
                tf.extractall(extract_to)
                extracted_files = [extract_to / name for name in tf.getnames()]
        
        print(f"      ✓ Extracted '{filepath.name}'.")
        filepath.unlink() # Delete the archive that was just extracted

        # Recursively check extracted files
        for item_path in extracted_files:
            if item_path.is_file():
                _extract_if_archive(item_path, item_path.parent, depth + 1)

    except Exception as e:
        print(f"      ✗ Extraction failed for '{filepath.name}': {e}")


def _get_full_extension(filepath: Path) -> str:
    """Get full extension including compound extensions like .tar.gz"""
    name = filepath.name.lower()
    
    if name.endswith('.tar.gz'):
        return '.tar.gz'
    elif name.endswith('.tar.bz2'):
        return '.tar.bz2'
    else:
        return filepath.suffix.lower()

def download_course(page: Page, course: dict, course_id: str, output_dir: str):
    """Downloads all graded assignments for one course."""
    print(f"\nProcessing course: {course['full_name']}")
    sanitized_name = "".join([c for c in course['full_name'] if c.isalnum() or c in ' -']).strip()
    course_path = Path(output_dir) / sanitized_name
    course_path.mkdir(parents=True, exist_ok=True)
    
    page.goto(course['url'])
    page.wait_for_load_state('networkidle')

    assignments = []
    # Find all rows in the assignment table
    for row in page.locator("table tbody tr").all():
        # Check the second column for "Graded" status or a score
        status_cell = row.locator("td:nth-child(2)")
        if status_cell.is_visible():
            status_text = status_cell.text_content().strip()
            if "Graded" in status_text or re.search(r'\d+(\.\d+)?\s*/\s*\d+(\.\d+)?', status_text):
                # Find the assignment link within the row
                link_element = row.locator('a[href*="/assignments/"]').first
                if link_element.is_visible():
                    href = link_element.get_attribute('href')
                    name = link_element.text_content().strip()
                    if href and name and not any(a['url'].endswith(href) for a in assignments):
                        assignments.append({'name': name, 'url': f"https://www.gradescope.com{href}"})
    
    print(f"Found {len(assignments)} assignments in {course['full_name']}.")

    for assignment in assignments:
        assignment_name = assignment['name']
        assignment_url = assignment['url']
        
        # Sanitize assignment name for directory creation
        sanitized_assignment_name = "".join([c for c in assignment_name if c.isalnum() or c in '._-']).strip()
        assignment_dir = course_path / sanitized_assignment_name
        
        download_assignment(page, assignment_name, assignment_url, assignment_dir)

    # After processing all assignments, update the timestamp
    gcm.update_course_timestamp(course_id)
    time.sleep(CONFIG['delay'])
    

GITHUB_USERNAME = None

def get_github_username():
    global GITHUB_USERNAME
    if GITHUB_USERNAME is None:
        try:
            result = subprocess.run(
                ['gh', 'api', 'user', '--jq', '.login'],
                check=True, capture_output=True, text=True
            )
            GITHUB_USERNAME =result.stdout.strip()
            print(f"  ✓ Fetched GitHub username: {GITHUB_USERNAME}")
        except subprocess.CalledProcessError as e:
            print(f"  ✗ Failed to fetch GitHub username: {e.stderr.strip()}")
            GITHUB_USERNAME = "" # Set to empty to avoid repeated attempts
    return GITHUB_USERNAME

# Modify rename_course_repo
def rename_course_repo(old_name: str, new_name: str, course_id: str):
    """
    Renames the GitHub repository and, if it exists, the local course directory.
    Uses the github_repo field stored in courses.json.
    """
    print(f"\n--- Renaming course: '{old_name}' -> '{new_name}' ---")

    courses_data = gcm.load_courses_from_json()
    if course_id not in courses_data:
        print(f"ERROR: Course ID '{course_id}' not found in JSON.")
        return False

    course_info = courses_data[course_id]
    old_repo_name = course_info.get('github_repo')
    if not old_repo_name:
        print(f"  - GitHub repo name for '{old_name}' not found in JSON. Assuming no repo exists.")
        del courses_data[course_id]
        gcm.save_courses_to_json(courses_data)
        print(f"  ✓ Removed course '{old_name}' from courses.json.")
        return True # Treat as a "successful" operation

    # Sanitize names for paths and repo
    sanitized_new_name = "".join([c for c in new_name if c.isalnum() or c in '-']).replace(' ', '-').strip()
    old_path = Path(CONFIG['output_dir']) / "".join([c for c in old_name if c.isalnum() or c in ' -']).strip()
    new_path = Path(CONFIG['output_dir']) / "".join([c for c in new_name if c.isalnum() or c in ' -']).strip()
    
    original_cwd = Path.cwd()
    
    try:
        # Part 1: Handle local directory path for running gh command
        run_dir = None
        if old_path.exists():
            if old_path != new_path:
                old_path.rename(new_path)
                print(f"  ✓ Local folder renamed to '{new_path.name}'")
            run_dir = new_path
        else:
            print(f"  - Local folder '{old_path}' does not exist. Skipping local rename.")
            run_dir = Path(CONFIG['output_dir'])
        
        os.chdir(run_dir)

        # Part 2: Handle GitHub repo rename
        try:
            github_username = get_github_username()
            if not github_username:
                print("  ✗ Cannot rename remote GitHub repo without username.")
                return False

            full_old_repo_path = f"{github_username}/{old_repo_name}"

            subprocess.run(
                ['gh', 'repo', 'rename', sanitized_new_name, '--repo', full_old_repo_path, '--yes'],
                check=True, capture_output=True, text=True
            )
            print(f"  ✓ GitHub repo renamed: {full_old_repo_path} -> {sanitized_new_name}")

            # Update JSON
            courses_data[course_id]['full_name'] = new_name
            courses_data[course_id]['github_repo'] = sanitized_new_name
            courses_data[course_id]['timestamp'] = datetime.now()
            courses_data[course_id]['rename'] = ""
            gcm.save_courses_to_json(courses_data)
            print("  ✓ Updated JSON with new course and repo name")

            return True
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.strip()
            if "404" in stderr or "Not Found" in stderr:
                print(f"  - GitHub repo {github_username}/{old_repo_name} not found. Assuming deleted.")
                # Remove the course from courses.json
                del courses_data[course_id]
                gcm.save_courses_to_json(courses_data)
                print(f"  ✓ Removed course '{old_name}' from courses.json.")
                return True # Treat as a "successful" operation
            else:
                print(f"✗ Failed to rename GitHub repo: {stderr}")
                return False
    finally:
        os.chdir(original_cwd)

def create_git_repo(course_dir: Path, course: dict):
    """
    Initialize a git repo for a course and push it to GitHub.
    Stores the GitHub repo name in courses.json for future reference.
    """
    course_name = course['full_name']
    print(f"\n--- Setting up Git repository for {course_name} ---")

    if not course_dir.is_dir():
        print(f"ERROR: Course directory '{course_dir}' not found.")
        return False

    original_cwd = Path.cwd()

    # 2️⃣ Sanitize GitHub repo name (do this BEFORE changing directory)
    sanitized_repo_name = "".join([c for c in course_name if c.isalnum() or c in '-']).replace(' ', '-').strip()

    try:
        # 1️⃣ Initialize git repo if it doesn't exist
        os.chdir(course_dir)
        if not (course_dir / ".git").exists():
            subprocess.run(['git', 'init'], check=True, capture_output=True)
            subprocess.run(['git', 'add', '.'], check=True, capture_output=True)
            if subprocess.run(['git', 'status', '--porcelain'], capture_output=True).stdout:
                subprocess.run(
                    ['git', 'commit', '-m', f"Initial commit: Gradescope archive for {course_name}"],
                    check=True, capture_output=True
                )
                print("  ✓ Git initialized and initial commit made.")
            else:
                print("  No changes to commit.")
        else:
            print("  Git repo already exists. Skipping init.")

        # 3️⃣ Create GitHub repo if remote 'origin' doesn't exist
        remotes = subprocess.run(['git', 'remote'], capture_output=True, text=True).stdout.split()
        if 'origin' not in remotes:
            try:
                # Determine visibility flag from CONFIG
                visibility_flag = '--private' if CONFIG.get('DEFAULT_REPO_PRIVATE', True) else '--public'
                
                subprocess.run(
                    ['gh', 'repo', 'create', sanitized_repo_name, visibility_flag, '--source=.', '--remote=origin'],
                    check=True, capture_output=True, text=True
                )
                print(f"  ✓ GitHub repo created: {sanitized_repo_name} ({visibility_flag.strip('--')})")
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.strip()
                if "Name already exists on this account" in stderr:
                    print(f"  - GitHub repo '{sanitized_repo_name}' already exists. Proceeding.")
                    github_username = _get_github_username()
                    remote_url = f"https://github.com/{github_username}/{sanitized_repo_name}.git"
                    subprocess.run(['git', 'remote', 'add', 'origin', remote_url], check=True)
                    print("  ✓ Added remote 'origin'.")
                else:
                    print(f"  ✗ Failed to create GitHub repo: {stderr}")
                    return False
        else:
            print("  Remote 'origin' already exists. Skipping creation.")

        # 4️⃣ Push to GitHub
        subprocess.run(['git', 'branch', '-M', 'main'], check=True, capture_output=True)
        subprocess.run(['git', 'push', '-u', 'origin', 'main', '--force'], check=True, capture_output=True)
        print(f"  ✓ Successfully pushed to GitHub: {sanitized_repo_name}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  ✗ An error occurred during git operations: {e}")
        return False
    finally:
        # IMPORTANT: Change back to original directory BEFORE updating JSON
        os.chdir(original_cwd)

    # 5️⃣ Store GitHub repo name in JSON (OUTSIDE the try/finally block, after chdir back)
    courses_data = gcm.load_courses_from_json()
    course_id = course['url']

    # Ensure there is an entry for this course in the JSON
    if course_id not in courses_data:
        raise ValueError(
            f"Course ID '{course_id}' not found in courses.json! "
            f"Please run --update-courses first to populate the courses database."
        )

    courses_data[course_id]['github_repo'] = sanitized_repo_name

    gcm.save_courses_to_json(courses_data)
    print(f"  ✓ Stored GitHub repo name in courses.json under ID '{course_id}'")
    return True


def interactive_workflow(page: Page):
    """Runs the archiver in an interactive loop safely."""
    while True:
        print("\n--- Gradescope Archiver Interactive Mode ---")
        all_courses = get_courses(page)
        if not all_courses:
            print("No courses found. Exiting.")
            break
        
        for i, c in enumerate(all_courses):
            print(f"{i+1}. {c['full_name']}")
        
        choice = input("\nEnter a number to process, or 'q' to quit: ").strip().lower()
        if choice == 'q':
            break
        
        try:
            course = all_courses[int(choice) - 1]
            
            # Download graded assignments
            course_id = course['url']  # Use the Gradescope course URL as the unique ID
            download_course(page, course, course_id, CONFIG['output_dir'])
            
            # Create and push Git repository
            sanitized_name = "".join(c if c.isalnum() or c in ' -' else '-' for c in course['full_name']).strip()
            repo_dir = Path(CONFIG['output_dir']) / sanitized_name
            success = create_git_repo(repo_dir, course)

            # Only offer delete if push succeeded
            if success:
                shutil.rmtree(repo_dir)
                print("Local directory deleted safely.")
        
        except (ValueError, IndexError):
            print("Invalid input. Please enter a valid number.")
        except Exception as e:
            print(f"An error occurred: {e}")
