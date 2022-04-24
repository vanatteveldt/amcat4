from typing import Optional

import requests
from fastapi.testclient import TestClient

from amcat4.auth import verify_token, verify_user, Role, User
from tests.tools import get_json, build_headers, post_json


def test_get_token(client: TestClient, user: User):
    check(client.post('/auth/token'), 422, "Getting a token requires a form")
    check(client.post('/auth/token',  data=dict(username=user.email, password='wrong')), 401)
    r = client.post('/auth/token', data=dict(username=user.email, password=user.plaintext_password))
    assert r.status_code == 200
    assert verify_token(r.json()['access_token']) == user


def test_get_user(client: TestClient, user: User, admin: User, username: str):
    """Test GET user functionality and authorization"""
    assert client.get(f"/users/me").status_code == 401

    # user can only see its own info:
    assert get_json(client, f"/users/{user.email}", user=user) == {"email": user.email, "global_role": None}
    assert client.get(f"/users/{admin.email}", headers=build_headers(user)).status_code == 401
    # admin can see everyone
    assert get_json(client, f"/users/{user.email}", user=admin) == {"email": user.email, "global_role": None}
    assert get_json(client, f"/users/{admin.email}", user=admin) == {"email": admin.email, "global_role": 'ADMIN'}
    assert client.get(f'/users/{username}', headers=build_headers(admin)).status_code == 404


def test_create_user(client: TestClient, user, writer, admin, username):
    # anonymous or unprivileged users cannot create new users
    assert client.post('/users/').status_code == 401, "Creating user should require auth"
    assert client.post("/users/", headers=build_headers(user)).status_code == 401, "Creating user should require >=WRITER"
    # writers can add new users
    u = dict(email=username, password="geheim")
    assert set(post_json(client, "/users/", user=writer, json=u).keys()) == {"email", "id"}
    assert client.post("/users/", headers=build_headers(writer), json=u).status_code == 400, "Duplicate create should return 400"
    # users can delete themselves, others cannot delete them
    assert client.delete(f"/users/{username}", headers=build_headers(user)).status_code == 401
    u = User.get(User.email == username)
    assert client.delete(f"/users/{username}", headers=build_headers(u)).status_code == 204
    # only admin can add admins
    u = dict(email=username, password="geheim", global_role='ADMIN')
    assert client.post("/users/", headers=build_headers(writer), json=u).status_code == 401, "Creating admins should require ADMIN"
    assert client.post("/users/", headers=build_headers(admin), json=u).status_code == 201
    assert get_json(client, f"/users/{username}", user=admin)["global_role"] == "ADMIN"
    # (only) admin can delete other admins
    assert client.delete(f"/users/{username}", headers=build_headers(writer)).status_code == 401
    assert client.delete(f"/users/{username}", headers=build_headers(admin)).status_code == 204


def check(response: requests.Response, expected: int, msg: Optional[str] = None):
    assert response.status_code == expected, \
        f"{msg}{': ' if msg else ''}Unexpected status: {response.status_code} != {expected}; reply: {response.json()}"


def test_modify_user(client: TestClient, user, writer, admin):
    """Are the API endpoints and auth for modifying users correct?"""
    # Normal users can change their own password
    check(client.put(f"/users/{user.email}", headers=build_headers(user), json={'password': 'x'}), 200)
    assert verify_user(user.email, 'x') == user

    # Anonymous or normal users can't change other users
    assert client.put(f"/users/{user.email}").status_code == 401, "Changing user requires AUTH"
    assert client.put(f"/users/{admin.email}", headers=build_headers(writer), json={'password': 'x'}).status_code == 401

    # Writers can change other users, but not admins
    assert client.put(f"/users/{user.email}", headers=build_headers(writer), json={'password': 'y'}).status_code == 200
    assert client.put(f"/users/{admin.email}", headers=build_headers(writer), json={'password': 'y'}).status_code == 401

    # You can change privileges of other users up to your own privilege
    check(client.put(f"/users/{user.email}", headers=build_headers(user), json={'global_role': 'writer'}), 401)
    check(client.put(f"/users/{user.email}", headers=build_headers(writer), json={'global_role': 'writer'}), 200)
    assert User.get_by_id(user.id).global_role == Role.WRITER
    assert client.put(f"/users/{user.email}", headers=build_headers(writer), json={'global_role': 'admin'}).status_code == 401
    assert client.put(f"/users/{writer.email}", headers=build_headers(admin), json={'global_role': 'admin'}).status_code == 200
    assert User.get_by_id(writer.id).global_role == Role.ADMIN
