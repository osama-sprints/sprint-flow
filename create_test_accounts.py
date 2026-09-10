#!/usr/bin/env python3
import json
import urllib.request
import urllib.error
import sys

MM_API = "http://localhost:8065/api/v4"
ADMIN_USER = "admin@sprints.ai"
ADMIN_PASS = "Admin123!"

def api_call(method, endpoint, data=None, token=None):
    url = f"{MM_API}{endpoint}"
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    
    body = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode('utf-8')
            res_json = json.loads(res_body) if res_body else {}
            # Check for Token header (Mattermost returns session token in header)
            token_header = response.getheader('Token')
            if token_header:
                res_json['_token'] = token_header
            return res_json
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except:
            return {'error': str(e)}

def main():
    print("Logging in as system admin...")
    login = api_call('POST', '/users/login', {'login_id': ADMIN_USER, 'password': ADMIN_PASS})
    if '_token' not in login:
        print("Failed to get token:", login)
        sys.exit(1)
    
    token = login['_token']
    
    # Create Teams
    print("Creating teams...")
    team1 = api_call('POST', '/teams', {'name': 'team-alpha', 'display_name': 'Team Alpha', 'type': 'O'}, token)
    if 'error' in team1:
        team1 = api_call('GET', '/teams/name/team-alpha', token=token)
    
    team2 = api_call('POST', '/teams', {'name': 'team-beta', 'display_name': 'Team Beta', 'type': 'O'}, token)
    if 'error' in team2:
        team2 = api_call('GET', '/teams/name/team-beta', token=token)
        
    team1_id = team1['id']
    team2_id = team2['id']

    # Create Users
    print("Creating users...")
    users_info = [
        {"email": "admin1@test.com", "username": "team1_admin", "password": "Password1!", "first_name": "Team1", "last_name": "Admin"},
        {"email": "admin2@test.com", "username": "team2_admin", "password": "Password1!", "first_name": "Team2", "last_name": "Admin"},
        {"email": "user1@test.com", "username": "channel_a_user", "password": "Password1!", "first_name": "ChanA", "last_name": "User"},
        {"email": "user2@test.com", "username": "channel_b_user", "password": "Password1!", "first_name": "ChanB", "last_name": "User"},
    ]
    
    created_users = {}
    for u in users_info:
        res = api_call('POST', '/users', u, token)
        if 'error' in res and 'already exists' in res.get('error', ''):
            res = api_call('POST', '/users/usernames', [u['username']], token)[0]
        created_users[u['username']] = res['id']

    # Add users to teams and set team admin roles
    print("Assigning users to teams...")
    api_call('POST', f'/teams/{team1_id}/members', {'team_id': team1_id, 'user_id': created_users['team1_admin']}, token)
    api_call('PUT', f'/teams/{team1_id}/members/{created_users["team1_admin"]}/roles', {'roles': 'team_admin team_user'}, token)
    
    api_call('POST', f'/teams/{team2_id}/members', {'team_id': team2_id, 'user_id': created_users['team2_admin']}, token)
    api_call('PUT', f'/teams/{team2_id}/members/{created_users["team2_admin"]}/roles', {'roles': 'team_admin team_user'}, token)
    
    api_call('POST', f'/teams/{team1_id}/members', {'team_id': team1_id, 'user_id': created_users['channel_a_user']}, token)
    api_call('POST', f'/teams/{team2_id}/members', {'team_id': team2_id, 'user_id': created_users['channel_b_user']}, token)

    # Create Channels
    print("Creating channels...")
    chan_a = api_call('POST', '/channels', {'team_id': team1_id, 'name': 'channel-a', 'display_name': 'Channel A', 'type': 'O'}, token)
    if 'error' in chan_a:
        chan_a = api_call('GET', f'/teams/{team1_id}/channels/name/channel-a', token=token)
        
    chan_b = api_call('POST', '/channels', {'team_id': team2_id, 'name': 'channel-b', 'display_name': 'Channel B', 'type': 'O'}, token)
    if 'error' in chan_b:
        chan_b = api_call('GET', f'/teams/{team2_id}/channels/name/channel-b', token=token)
        
    # Add users to channels
    print("Adding users to channels...")
    api_call('POST', f'/channels/{chan_a["id"]}/members', {'user_id': created_users['channel_a_user']}, token)
    api_call('POST', f'/channels/{chan_b["id"]}/members', {'user_id': created_users['channel_b_user']}, token)
    
    print("\n--- Setup Complete ---")
    print("Accounts created:")
    print("1. Team 1 Admin: username: team1_admin / password: Password1!")
    print("2. Team 2 Admin: username: team2_admin / password: Password1!")
    print("3. Channel A User (in Team 1): username: channel_a_user / password: Password1!")
    print("4. Channel B User (in Team 2): username: channel_b_user / password: Password1!")

if __name__ == '__main__':
    main()
