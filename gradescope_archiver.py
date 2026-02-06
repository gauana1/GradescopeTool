import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright
from datetime import datetime
import json
import shutil
import subprocess
import gradescope_lib as gs_lib
import gradescope_course_manager as gcm

def main():
    parser = argparse.ArgumentParser(
        description="Gradescope Course Archiver. Run with --interactive for a guided experience.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument('--setup', action='store_true', help='Perform manual login and save session state.')
    parser.add_argument('--interactive', action='store_true', help='Run in interactive mode to select courses one-by-one.')
    parser.add_argument('--download-all', action='store_true', help='Download all courses and assignments (non-interactive).')
    parser.add_argument('--test-course', type=str, help='Download a single course by its full name.')
    parser.add_argument('--update-courses', action='store_true', help='Update the courses.json file with the latest course list.')
    parser.add_argument('--update-stale-courses', action='store_true', help='Re-download courses that have not been updated recently.')
    parser.add_argument('--rename-courses', action='store_true', help='Rename local course directories based on the "rename" field in courses.json.')
    parser.add_argument('--nuke-all', action='store_true', help='Delete all GitHub repositories listed in courses.json.')
    args = parser.parse_args()

    if args.setup:
        gs_lib.setup_auth()
        return
        
    if args.rename_courses:
        print("--- Renaming Courses (no web interaction) ---")
        courses_data = gcm.load_courses_from_json()
        if not courses_data:
            print("courses.json is empty. Nothing to rename.")
            return
            
        for course_id, course_data in courses_data.items():
            if course_data.get('rename'):
                print(f"Renaming course: '{course_data['full_name']}' to '{course_data['rename']}'")
                rename_successful = gs_lib.rename_course_repo(course_data['full_name'], course_data['rename'], course_id)
                if rename_successful:
                    gcm.rename_course_in_json(course_id, course_data['rename'])
        
        print("\n--- Course renaming finished. ---")
        return

    if args.nuke_all:
        print("--- WARNING: This will permanently delete all GitHub repositories listed in courses.json. ---")
        if input("Are you sure you want to continue? (y/n): ").strip().lower() != 'y':
            print("Nuke aborted.")
            return

        courses_data = gcm.load_courses_from_json()
        if not courses_data:
            print("courses.json is empty. Nothing to nuke.")
            return

        github_username = gs_lib.get_github_username()
        if not github_username:
            print("Could not get GitHub username. Aborting nuke.")
            return

        for course_id, course_data in courses_data.items():
            repo_name = course_data.get('github_repo')
            if repo_name:
                full_repo_path = f"{github_username}/{repo_name}"
                print(f"  - Deleting GitHub repo: {full_repo_path}")
                try:
                    subprocess.run(
                        ['gh', 'repo', 'delete', full_repo_path, '--yes'],
                        check=True, capture_output=True, text=True
                    )
                    print(f"    ✓ Deleted {full_repo_path}")
                    courses_data[course_id]['github_repo'] = ""
                except subprocess.CalledProcessError as e:
                    stderr = e.stderr.strip()
                    if "404" in stderr or "Not Found" in stderr:
                        print(f"    - Repo {full_repo_path} not found on GitHub. Skipping.")
                        courses_data[course_id]['github_repo'] = "" # Still clear it
                    else:
                        print(f"    ✗ Failed to delete {full_repo_path}: {stderr}")
        
        gcm.save_courses_to_json(courses_data)
        print("\n--- Nuke operation finished. ---")
        return

    if not Path(gs_lib.CONFIG['auth_file']).exists():
        print(f"Authentication file '{gs_lib.CONFIG['auth_file']}' not found. Please run with --setup first.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=gs_lib.CONFIG['headless'],
            args=['--disable-extensions', '--disable-pdf-extension']
        )
        context = browser.new_context(storage_state=gs_lib.CONFIG['auth_file'])
        page = context.new_page()

        if args.interactive:
            gs_lib.interactive_workflow(page)
        elif args.download_all:
            print("--- Starting Download All Mode ---")
            all_courses = gs_lib.get_courses(page)
            for course in all_courses:
                gs_lib.download_course(page, course, course['url'], gs_lib.CONFIG['output_dir'])
                
                sanitized_name = "".join([c for c in course['full_name'] if c.isalnum() or c in ' -']).strip()
                course_dir = Path(gs_lib.CONFIG['output_dir']) / sanitized_name
                success = gs_lib.create_git_repo(course_dir, course)

                if success:
                    shutil.rmtree(course_dir)
                    print(f"Local directory for {course['full_name']} deleted safely.")
 
            print("\n--- All courses have been processed. ---")
        elif args.test_course:
            print(f"--- Testing download for course: {args.test_course} ---")
            all_courses = gs_lib.get_courses(page)
            target_course = next((c for c in all_courses if c['full_name'] == args.test_course), None)
            
            if target_course:
                gs_lib.download_course(page, target_course, target_course['url'], gs_lib.CONFIG['output_dir'])
                
                sanitized_name = "".join([c for c in target_course['full_name'] if c.isalnum() or c in ' -']).strip()
                course_dir = Path(gs_lib.CONFIG['output_dir']) / sanitized_name
                
                # ✅ Create Git repo so JSON gets updated
                gs_lib.create_git_repo(course_dir, target_course)
                
                print(f"\n--- Test download finished for {args.test_course}. ---")
            else:
                print(f"ERROR: Course '{args.test_course}' not found.")
                print("Please make sure you are using the exact full name from the course list.")
        elif args.update_courses:
            print("--- Updating courses.json ---")
            all_courses = gs_lib.get_courses(page)
            gcm.update_course_data(all_courses) # Now just updates the file
            updated_courses_for_display = gcm.load_courses_from_json() # Reload for display
            print("\n--- courses.json content: ---")
            # Custom encoder to handle datetime objects for printing
            class DateTimeEncoder(json.JSONEncoder):
                def default(self, obj):
                    if isinstance(obj, datetime):
                        return obj.isoformat()
                    return json.JSONEncoder.default(self, obj)
            print(json.dumps(updated_courses_for_display, indent=4, cls=DateTimeEncoder))
        elif args.update_stale_courses:
            print("--- Updating Stale Courses ---")
            courses_data = gcm.load_courses_from_json()
            if not courses_data:
                print("courses.json is empty. Run with --update-courses first.")
                return

            for course_id, course_data in courses_data.items():
                # Handle conditional update
                time_since_update = datetime.now() - course_data['timestamp']
                if time_since_update.total_seconds() > gs_lib.CONFIG['update_threshold_hours'] * 3600:
                    print(f"Course '{course_data['full_name']}' is older than {gs_lib.CONFIG['update_threshold_hours']} hours. Re-downloading...")
                    gs_lib.download_course(page, course_data, course_id, gs_lib.CONFIG['output_dir'])
                    
                    sanitized_name = "".join([c for c in course_data['full_name'] if c.isalnum() or c in ' -']).strip()
                    course_dir = Path(gs_lib.CONFIG['output_dir']) / sanitized_name
                    gs_lib.create_git_repo(course_dir, course_data)
            
            print("\n--- Stale course update finished. ---")
        else:
            print("--- Listing All Discovered Courses (run with --interactive, --test-course, --update-courses, or --update-stale-courses) ---")
            all_courses = gs_lib.get_courses(page)
            if all_courses:
                for course in all_courses:
                    print(f"- {course['full_name']}")
        
        browser.close()
        print("\nDone.")

if __name__ == '__main__':
    main()
