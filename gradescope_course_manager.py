import json
from pathlib import Path
from datetime import datetime

COURSES_FILE = "courses.json"

def load_courses_from_json():
    """Loads the course data from courses.json if it exists."""
    if Path(COURSES_FILE).exists():
        with open(COURSES_FILE, 'r') as f:
            data = json.load(f)
            # Convert timestamp strings back to datetime objects on load
            for course_id, course_info in data.items():
                if 'timestamp' in course_info and isinstance(course_info['timestamp'], str):
                    course_info['timestamp'] = datetime.fromisoformat(course_info['timestamp'])
            return data
    return {}

def save_courses_to_json(courses_data):
    """Saves the course data to courses.json."""
    # Convert datetime objects to ISO-formatted strings for saving
    serializable_data = {}
    for course_id, course_info in courses_data.items():
        serializable_info = course_info.copy()
        if 'timestamp' in serializable_info and isinstance(serializable_info['timestamp'], datetime):
            serializable_info['timestamp'] = serializable_info['timestamp'].isoformat()
        serializable_data[course_id] = serializable_info

    with open(COURSES_FILE, 'w') as f:
        json.dump(serializable_data, f, indent=4)

def update_course_data(discovered_courses):
    """
    Updates the courses.json file with newly discovered courses.
    """
    courses_data = load_courses_from_json()
    new_courses_added = 0
    
    for course in discovered_courses:
        course_id = course['url'] # Use URL as a unique ID
        if course_id not in courses_data:
            new_courses_added += 1
            courses_data[course_id] = {
                'full_name': course['full_name'],
                'short_name': course['short_name'],
                'term': course['term'],
                'url': course['url'],
                'timestamp': datetime.now(),
                'rename': ""
            }
            
    if new_courses_added > 0:
        save_courses_to_json(courses_data)
        print(f"Added {new_courses_added} new course(s) to {COURSES_FILE}.")
    else:
        print("No new courses found to add.")
        
    return courses_data

def update_course_timestamp(course_id: str):
    """Updates the timestamp for a specific course in courses.json."""
    courses_data = load_courses_from_json()
    if course_id in courses_data:
        courses_data[course_id]['timestamp'] = datetime.now()
        save_courses_to_json(courses_data)
        print(f"Updated timestamp for course: {courses_data[course_id]['full_name']}")

def rename_course_in_json(course_id: str, new_name: str):
    """Renames a course in courses.json and clears the 'rename' field."""
    courses_data = load_courses_from_json()
    if course_id in courses_data:
        courses_data[course_id]['full_name'] = new_name
        courses_data[course_id]['rename'] = ""
        courses_data[course_id]['timestamp'] = datetime.now() # Also update timestamp on rename
        save_courses_to_json(courses_data)
        print(f"Renamed course to '{new_name}' in {COURSES_FILE}.")
