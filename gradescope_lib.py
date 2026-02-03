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
import json
import time
import os

CONFIG = {
    'output_dir': 'gradescope_archive',
    'auth_file': 'gradescope_auth.json',
    'delay': 2,
    'headless': False,
    'max_retries': 3,
    'update_threshold_hours': 24
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
    """Return list of course dicts with parsed name components."""
    print("Discovering courses...")
    
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
    
    courses = []
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
            
            courses.append({
                'url': url,
                'full_name': full_name,
                'short_name': short_name,
                'term': term
            })
            
        except Exception as e:
            # Log but don't fail on individual course extraction errors
            print(f"    Warning: Failed to extract course info: {e}")
            continue
    
    print(f"Found {len(courses)} courses.")
    return courses

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


def _try_direct_downloads(page: Page, assignment_name: str, assignment_dir: Path) -> int:
    """Attempt to download all available files directly. Returns the count of successful downloads."""
    print("    Looking for direct download links...")
    
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
        'a[href$=".pdf"]', # Added to handle PDFs here
        'a:has-text("Download Graded Copy")', # Specific selector for graded PDF
    ]
    
    successful_downloads = 0
    
    # Use a set to track already processed URLs to avoid redundant downloads if multiple selectors match the same link
    downloaded_urls = set()

    for selector in direct_download_selectors:
        links = page.locator(selector).all()
        
        for i, link in enumerate(links):
            try:
                href = link.get_attribute('href')
                if not href or href in downloaded_urls:
                    continue # Skip if no href or already processed
                
                print(f"    Attempting download {i+1} (selector: '{selector}', href: '{href[:50]}...')")
                
                with page.expect_download(timeout=15000) as d_info:
                    link.click()
                
                download = d_info.value
                filename = download.suggested_filename
                filepath = assignment_dir / filename
                download.save_as(filepath)
                
                print(f"      ✓ Downloaded: '{filename}'")
                successful_downloads += 1
                downloaded_urls.add(href) # Mark this URL as downloaded
                
                # Extract if it's an archive
                _extract_if_archive(filepath, assignment_dir)
                
            except Exception as e:
                print(f"      ✗ Download failed for link (selector: '{selector}', href: '{href[:50] if href else 'N/A'}'). Details: {str(e)[:100]}")
                continue
    
    # Fallback: Also attempt to download graded PDF using requests if no Playwright download was triggered
    # This acts as a robust fallback for "Download Graded Copy" if the click above fails to trigger a Playwright download
    if successful_downloads == 0:
        if _try_graded_pdf_download_requests(page, assignment_name, assignment_dir):
            successful_downloads += 1

    return successful_downloads


def _extract_if_archive(filepath: Path, extract_to: Path):
    """Extract archive and recursively extract nested archives."""
    ext = _get_full_extension(filepath)
    
    if ext not in ['.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2']:
        return  # Not an archive
    
    print(f"      Detected archive: {ext}. Extracting...")
    
    try:
        if ext == '.zip':
            with zipfile.ZipFile(filepath, 'r') as zf:
                zf.extractall(extract_to)
        else:  # Various tar formats
            with tarfile.open(filepath, 'r:*') as tf:
                tf.extractall(extract_to)
        
        print(f"      ✓ Extracted to '{extract_to}'")
        filepath.unlink()  # Delete the archive
        print(f"      Deleted original archive: '{filepath.name}'")
        
        # Extract nested archives
        _extract_nested_archives(extract_to)
        
    except Exception as e:
        print(f"      ✗ Extraction failed: {e}")


def _extract_nested_archives(directory: Path):
    """Recursively find and extract nested archives."""
    print("      Scanning for nested archives...")
    
    # Collect all archive files first (don't modify while walking)
    archives = []
    for root, dirs, files in os.walk(directory):
        for filename in files:
            filepath = Path(root) / filename
            ext = _get_full_extension(filepath)
            if ext in ['.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2']:
                archives.append(filepath)
    
    # Now extract them
    for archive_path in archives:
        print(f"        Found nested archive: {archive_path.name}")
        try:
            ext = _get_full_extension(archive_path)
            extract_dir = archive_path.parent
            
            if ext == '.zip':
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    zf.extractall(extract_dir)
            else:
                with tarfile.open(archive_path, 'r:*') as tf:
                    tf.extractall(extract_dir)
            
            print(f"        ✓ Extracted nested archive")
            archive_path.unlink()
            print(f"        Deleted: {archive_path.name}")
            
        except Exception as e:
            print(f"        ✗ Failed to extract {archive_path.name}: {e}")


def _get_full_extension(filepath: Path) -> str:
    """Get full extension including compound extensions like .tar.gz"""
    name = filepath.name.lower()
    
    if name.endswith('.tar.gz'):
        return '.tar.gz'
    elif name.endswith('.tar.bz2'):
        return '.tar.bz2'
    else:
        return filepath.suffix.lower()


def _try_graded_pdf_download_requests(page: Page, assignment_name: str, assignment_dir: Path) -> bool:
    """Attempt to download the graded PDF directly via requests, with retries. Returns True if successful."""
    for i in range(CONFIG['max_retries']):
        try:
            download_link_locator = page.get_by_role("link", name="Download Graded Copy")
            pdf_url = download_link_locator.get_attribute('href', timeout=2000)
            
            if not pdf_url:
                print("      ✗ Could not extract PDF URL for requests download.")
                return False
            
            if pdf_url.startswith('/'):
                pdf_url = f"https://www.gradescope.com{pdf_url}"
            
            cookies = {c['name']: c['value'] for c in page.context.cookies()}
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                             'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'
            }
            
            print(f"    Downloading PDF directly via requests from: {pdf_url[:60]}...")
            response = requests.get(pdf_url, cookies=cookies, headers=headers, allow_redirects=True)
            
            if 500 <= response.status_code < 600:
                print(f"      ! Server error ({response.status_code}). Retrying in {CONFIG['delay']}s... (Attempt {i+1}/{CONFIG['max_retries']})")
                time.sleep(CONFIG['delay'])
                continue # Go to the next iteration of the retry loop
            
            response.raise_for_status() # Raise an exception for other bad status codes (4xx)
            
            safe_name = "".join(c for c in assignment_name if c.isalnum() or c in '._- ').strip()
            filename = f"{safe_name}_graded.pdf"
            filepath = assignment_dir / filename
            
            filepath.write_bytes(response.content)
            print(f"      ✓ Saved (requests): '{filename}'")
            return True # Success
            
        except Exception as e:
            print(f"      ✗ PDF download (requests) failed: {e}")
            if i < CONFIG['max_retries'] - 1:
                time.sleep(CONFIG['delay'])
            continue # Go to the next iteration of the retry loop

    print(f"      ✗ PDF download failed after {CONFIG['max_retries']} retries.")
    return False
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

def rename_course_repo(old_name: str, new_name: str, course_id: str):
    """
    Creates a new, renamed copy of a course archive with its own new GitHub repository.
    The original directory and repository are left untouched.
    """
    print(f"--- Creating a new, renamed course archive: '{new_name}' from '{old_name}' ---")

    # 1. Check for gh CLI
    if shutil.which("gh") is None:
        print("  ERROR: The 'gh' command-line tool is not installed or not in your PATH.")
        print("         Please install it to use this feature: https://cli.github.com/")
        return

    # 2. Sanitize names
    sanitized_old_name = "".join([c for c in old_name if c.isalnum() or c in ' -']).strip()
    sanitized_new_name = "".join([c for c in new_name if c.isalnum() or c in ' -']).strip()
    
    old_path = Path(CONFIG['output_dir']) / sanitized_old_name
    new_path = Path(CONFIG['output_dir']) / sanitized_new_name
    
    if not old_path.exists():
        print(f"  ERROR: Original directory '{old_path}' not found. Cannot create a renamed copy.")
        return
        
    if new_path.exists():
        print(f"  ERROR: A directory already exists at '{new_path}'. Please choose a different name.")
        return

    original_cwd = os.getcwd()
    try:
        # 3. Copy the directory
        shutil.copytree(old_path, new_path)
        print(f"  ✓ Successfully copied '{old_path}' to '{new_path}'.")
        
        # 4. Navigate into the new directory and create a new Git repo
        os.chdir(new_path)
        
        # Remove the old .git directory if it was copied
        old_git_dir = new_path / '.git'
        if old_git_dir.exists():
            shutil.rmtree(old_git_dir)
            print("  - Removed old .git directory from the new folder.")

        # Initialize a new Git repository
        subprocess.run(['git', 'init'], check=True, capture_output=True)
        subprocess.run(['git', 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', f"Initial commit for renamed course: {new_name}"], check=True, capture_output=True)
        print(f"  ✓ Initialized a new Git repository in '{new_path}'.")

        # 5. Create a new remote GitHub repository
        new_repo_name = "".join([c for c in new_name if c.isalnum() or c in '-']).strip().replace(' ', '-')
        print(f"  Creating new PUBLIC GitHub repository '{new_repo_name}'...")
        
        try:
            subprocess.run(['gh', 'repo', 'create', new_repo_name, '--public', '--source=.', '--remote=origin'], check=True, capture_output=True, text=True)
            print(f"  ✓ Successfully created remote GitHub repository '{new_repo_name}'.")
            
            # Push to the new remote
            print("  Pushing to new GitHub repository...")
            subprocess.run(['git', 'branch', '-M', 'main'], check=True, capture_output=True)
            subprocess.run(['git', 'push', '-u', 'origin', 'main'], check=True, capture_output=True)
            print("  ✓ Successfully pushed to new repository.")
            
        except subprocess.CalledProcessError as e:
            print(f"  ✗ ERROR: Failed to create or push to GitHub repository. Details: {e.stderr.strip()}")
            print("           Please ensure 'gh' CLI is installed and authenticated.")
            print("           You may need to manually create the repo and push.")
            
        # 6. Update the course data in JSON
        gcm.rename_course_in_json(course_id, new_name)
        
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"  ✗ ERROR: A Git or file operation failed. Details: {e}")
        if isinstance(e, subprocess.CalledProcessError):
            print(f"     Stderr: {e.stderr}")
    except Exception as e:
        print(f"  ✗ ERROR: An unexpected error occurred. Details: {e}")
    finally:
        os.chdir(original_cwd)

def create_git_repo(course_dir: Path, course_name: str):
    """Initializes and pushes a git repository for a course."""
    print(f"\n--- Setting up Git repository for {course_name} ---")
    if not course_dir.is_dir():
        print(f"ERROR: Course directory '{course_dir}' not found.")
        return
        
    original_cwd = os.getcwd()
    os.chdir(course_dir)
    try:
        # Check if already a git repo
        if (Path(course_dir) / '.git').exists():
            print(f"  {course_dir.name} is already a Git repository. Skipping initialization.")
        else:
            subprocess.run(['git', 'init'], check=True, capture_output=True)
            subprocess.run(['git', 'add', '.'], check=True, capture_output=True)
            if subprocess.run(['git', 'status', '--porcelain'], capture_output=True).stdout:
                subprocess.run(['git', 'commit', '-m', f"Initial commit: Gradescope archive for {course_name}"], check=True, capture_output=True)
                print(f"  Git repository initialized and initial commit made at {course_dir}")
            else:
                print(f"  No changes to commit for {course_name}.")
        
        # Create GitHub repository and push
        sanitized_repo_name = "".join([c for c in course_name if c.isalnum() or c in '-']).strip().replace(' ', '-')
        
        # Check if remote named 'origin' exists
        result = subprocess.run(['git', 'remote', '-v'], capture_output=True, text=True)
        if "origin" not in result.stdout:
            print(f"  Creating PUBLIC GitHub repository '{sanitized_repo_name}'...")
            try:
                subprocess.run(['gh', 'repo', 'create', sanitized_repo_name, '--public', '--source=.', '--remote=origin'], check=True, capture_output=True, text=True)
                print(f"  ✓ Successfully created remote GitHub repository '{sanitized_repo_name}'.")
            except subprocess.CalledProcessError as e:
                print(f"  ✗ ERROR: Failed to create GitHub repository. Details: {e.stderr.strip()}")
                print("           Please ensure 'gh' CLI is installed and authenticated.")
                # Continue without pushing if repo creation fails
                return
        else:
            print(f"  Remote 'origin' already exists. Skipping remote repository creation.")

        print("  Pushing to GitHub...")
        subprocess.run(['git', 'branch', '-M', 'main'], check=True, capture_output=True)
        subprocess.run(['git', 'push', '-u', 'origin', 'main', '--force'], check=True, capture_output=True)
        print(f"  ✓ Successfully pushed to GitHub repository: {sanitized_repo_name}")
        
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"  ✗ ERROR: Git/GitHub operation failed for {course_name}. Details: {e}")
        if isinstance(e, subprocess.CalledProcessError):
            print(f"     Stdout: {e.stdout.decode().strip()}")
            print(f"     Stderr: {e.stderr.decode().strip()}")
        print("           Please ensure 'gh' CLI is installed and authenticated and Git is configured.")
    except Exception as e:
        print(f"  ✗ ERROR: An unexpected error occurred during Git operations. Details: {e}")
    finally:
        os.chdir(original_cwd)

def interactive_workflow(page: Page):
    """Runs the archiver in an interactive loop."""
    while True:
        print("\n--- Gradescope Archiver Interactive Mode ---")
        all_courses = get_courses(page)
        if not all_courses: break
        for i, c in enumerate(all_courses): print(f"{i+1}. {c['full_name']}")
        choice = input("\nEnter a number to process, or 'q' to quit: ").strip().lower()
        if choice == 'q': break
        try:
            course = all_courses[int(choice) - 1]
            download_course(page, course, CONFIG['output_dir'])
            if input("Create and push Git repository? (y/n): ").lower() == 'y':
                sanitized_name = "".join([c for c in course['full_name'] if c.isalnum() or c in ' -']).strip()
                create_git_repo(Path(CONFIG['output_dir']) / sanitized_name, course['full_name'])
            if input("Delete local folder after push? (y/n): ").lower() == 'y':
                shutil.rmtree(Path(CONFIG['output_dir']) / sanitized_name)
                print("Local directory deleted.")
        except (ValueError, IndexError):
            print("Invalid input.")
        except Exception as e:
            print(f"An error occurred: {e}")