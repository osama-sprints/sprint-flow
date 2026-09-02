#!/usr/bin/env python3
"""Bootstrap Mattermost via REST API"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

# Load .env
env_file = '.env'
env_vars = {}
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                env_vars[key] = val.strip('"\'')

MM_PORT = env_vars.get('MATTERMOST_HOST_PORT', '8065')
MM_API = f"http://localhost:{MM_PORT}/api/v4"
MM_ADMIN_USERNAME = env_vars.get('MM_ADMIN_USERNAME', 'admin')
MM_ADMIN_PASSWORD = env_vars.get('MM_ADMIN_PASSWORD', 'Admin123!')
MM_ADMIN_EMAIL = env_vars.get('MM_ADMIN_EMAIL', 'admin@sprints.ai')
MM_TEAM_NAME = env_vars.get('MM_TEAM_NAME', 'sprints-community')
MM_TEAM_DISPLAY_NAME = env_vars.get('MM_TEAM_DISPLAY_NAME', 'Sprints Community')
BOT_USERNAME = env_vars.get('MATTERMOST_BOT_USERNAME', 'sprintflow-assistant')
MM_BOT_CHANNEL = env_vars.get('MM_BOT_CHANNEL', 'town-square')
TRIGGER_WORDS = env_vars.get('MATTERMOST_TRIGGER_WORDS', '@sprintflow-assistant,!ask')

def api_call(method, endpoint, data=None, token=None):
    """Make API call to Mattermost"""
    url = f"{MM_API}{endpoint}"
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    
    body = None
    if data:
        body = json.dumps(data).encode('utf-8')
    
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except:
            return {'error': str(e)}

def wait_for_mattermost():
    """Wait for Mattermost to be ready"""
    print(f"Waiting for Mattermost on port {MM_PORT}...")
    for i in range(60):
        try:
            response = urllib.request.urlopen(f"{MM_API}/system/ping", timeout=2)
            print("✓ Mattermost is responding")
            return True
        except:
            if i < 59:
                time.sleep(2)
    print("ERROR: Mattermost did not respond")
    return False

def main():
    if not wait_for_mattermost():
        sys.exit(1)
    
    # Create admin user
    print(f"Creating admin user '{MM_ADMIN_USERNAME}'...")
    admin_user = api_call('POST', '/users', {
        'email': MM_ADMIN_EMAIL,
        'username': MM_ADMIN_USERNAME,
        'password': MM_ADMIN_PASSWORD,
        'first_name': 'Admin',
        'last_name': 'User'
    })
    
    if 'error' in admin_user and 'already exists' not in admin_user.get('error', ''):
        print(f"  ! {admin_user.get('message', 'Could not create admin')}")
    elif 'id' in admin_user:
        print(f"  ✓ Admin user created")
        admin_id = admin_user['id']
        # Promote to admin
        api_call('PUT', f'/users/{admin_id}/roles', {'roles': 'system_admin system_user'})
    
    # Login as admin
    print("Authenticating as admin...")
    login_result = api_call('POST', '/users/login', {
        'login_id': MM_ADMIN_USERNAME,
        'password': MM_ADMIN_PASSWORD
    })
    
    if 'id' not in login_result:
        print(f"  ERROR: Login failed: {login_result}")
        sys.exit(1)
    
    admin_token = login_result.get('token')
    if not admin_token:
        print("  ERROR: No token received")
        sys.exit(1)
    
    print(f"  ✓ Authenticated")
    
    # Create team
    print(f"Creating team '{MM_TEAM_DISPLAY_NAME}'...")
    team = api_call('POST', '/teams', {
        'name': MM_TEAM_NAME,
        'display_name': MM_TEAM_DISPLAY_NAME,
        'type': 'O'
    }, admin_token)
    
    if 'error' in team and 'already exists' not in team.get('error', ''):
        print(f"  ! Team may already exist")
    elif 'id' in team:
        print(f"  ✓ Team created")
    
    team_id = team.get('id')
    if not team_id:
        # Try to get existing team
        team_lookup = api_call('GET', f'/teams/name/{MM_TEAM_NAME}', token=admin_token)
        team_id = team_lookup.get('id')
    
    if not team_id:
        print("ERROR: Could not get team ID")
        sys.exit(1)
    
    print(f"✓ Bootstrap complete!")
    print(f"")
    print(f"Mattermost is ready:")
    print(f"  URL: http://localhost:{MM_PORT}")
    print(f"  Admin: {MM_ADMIN_USERNAME} / {MM_ADMIN_PASSWORD}")
    print(f"  Team: {MM_TEAM_DISPLAY_NAME}")

if __name__ == '__main__':
    main()
