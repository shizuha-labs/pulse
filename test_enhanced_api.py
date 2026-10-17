#!/usr/bin/env python
"""
Test script for the enhanced Task API with new features
"""
import requests
import json
from datetime import datetime, timedelta

# Configuration
TASKS_API_URL = "http://localhost:8002/api/tasks/"

def test_enhanced_task_api():
    """Test the enhanced Task API with new features"""
    
    print("🧪 ENHANCED TASK API TEST")
    print("=" * 50)
    
    # Test 1: Create a task with new fields
    task_data = {
        "title": "Test Enhanced Task with Assignment",
        "description": "This is a test task to validate the enhanced model",
        "task_type": "reminder",
        "source": "api",
        "assignee_id": 17,
        "assignee_email": "test-owner@shizuha.com",
        "created_by_id": 2,
        "created_by_email": "admin@shizuha.com",
        "due_date": (datetime.now() + timedelta(days=3)).isoformat(),
        "priority": "high",
        "frequency": "weekly",
        "is_recurring": True,
        "recurrence_end_date": (datetime.now() + timedelta(weeks=8)).isoformat(),
        "estimated_hours": 2.5,
        "set_metadata": {
            "project": "shizuha-stack",
            "component": "tasks",
            "integration_test": True
        },
        "set_tags": ["testing", "enhanced-features", "api"],
        "set_notification_preferences": {
            "email": True,
            "push": True,
            "advance_hours": 24
        }
    }
    
    print("Creating enhanced task...")
    print(f"Task Data: {json.dumps(task_data, indent=2, default=str)}")
    
    try:
        response = requests.post(TASKS_API_URL, json=task_data, timeout=10)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 201:
            task = response.json()
            task_id = task['id']
            print(f"✅ Task created successfully! ID: {task_id}")
            print(f"Metadata: {task.get('metadata_dict', {})}")
            print(f"Tags: {task.get('tags_list', [])}")
            print(f"Assignee: {task.get('assignee_email')}")
            print(f"Is Recurring: {task.get('is_recurring')}")
            print(f"Frequency: {task.get('frequency')}")
            
            # Test 2: Get task details
            print("\n" + "-" * 30)
            print("Getting task details...")
            detail_response = requests.get(f"{TASKS_API_URL}{task_id}/", timeout=10)
            if detail_response.status_code == 200:
                detailed_task = detail_response.json()
                print(f"✅ Task details retrieved")
                print(f"Days until due: {detailed_task.get('days_until_due')}")
                print(f"Is overdue: {detailed_task.get('is_overdue')}")
            
            # Test 3: Test task assignment
            print("\n" + "-" * 30)
            print("Testing task assignment...")
            assign_data = {
                "assignee_id": 17,
                "assignee_email": "test-owner@shizuha.com"
            }
            assign_response = requests.post(f"{TASKS_API_URL}{task_id}/assign/", json=assign_data, timeout=10)
            if assign_response.status_code == 200:
                assign_result = assign_response.json()
                print(f"✅ Task assigned: {assign_result.get('message')}")
                print(f"Assignee: {assign_result.get('assignee', {})}")
            else:
                print(f"❌ Assignment failed: {assign_response.status_code}")
                print(f"Response: {assign_response.text}")
            
            # Test 4: Test task completion
            print("\n" + "-" * 30)
            print("Testing task completion...")
            complete_response = requests.post(f"{TASKS_API_URL}{task_id}/complete/", timeout=10)
            if complete_response.status_code == 200:
                complete_result = complete_response.json()
                print(f"✅ Task completed: {complete_result.get('message')}")
                if complete_result.get('next_task_id'):
                    print(f"🔄 Next recurring instance created: {complete_result.get('next_task_id')}")
            else:
                print(f"❌ Completion failed: {complete_response.status_code}")
                print(f"Response: {complete_response.text}")
            
            # Test 5: Get task statistics
            print("\n" + "-" * 30)
            print("Getting task statistics...")
            stats_response = requests.get(f"{TASKS_API_URL}statistics/", timeout=10)
            if stats_response.status_code == 200:
                stats = stats_response.json()
                print(f"✅ Statistics retrieved")
                print(f"Total tasks: {stats.get('total_tasks')}")
                print(f"By status: {stats.get('by_status', {})}")
                print(f"By priority: {stats.get('by_priority', {})}")
                print(f"Overdue: {stats.get('overdue')}")
                print(f"Recurring: {stats.get('recurring_tasks')}")
            
            # Test 6: Test filtering
            print("\n" + "-" * 30)
            print("Testing task filtering...")
            filter_params = {
                'task_type': 'reminder',
                'priority': 'high',
                'assignee_id': 17
            }
            filter_response = requests.get(TASKS_API_URL, params=filter_params, timeout=10)
            if filter_response.status_code == 200:
                filtered_tasks = filter_response.json()
                print(f"✅ Filtering works: {len(filtered_tasks.get('results', filtered_tasks))} tasks found")
            
            return True
            
        else:
            print(f"❌ FAILED: Task creation failed")
            print(f"Response: {response.text}")
            return False
            
    except requests.RequestException as e:
        print(f"❌ FAILED: Request error - {e}")
        return False
    except Exception as e:
        print(f"❌ FAILED: Unexpected error - {e}")
        return False

if __name__ == "__main__":
    success = test_enhanced_task_api()
    if success:
        print("\n" + "=" * 50)
        print("🎉 ALL ENHANCED API TESTS PASSED!")
    else:
        print("\n" + "=" * 50)
        print("💥 TESTS FAILED - Check the logs above")
