"""Verify Mattermost REST API access from inside the ai-core container."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx
from dotenv import load_dotenv


load_dotenv("/app/.env")
HTTP_TIMEOUT = float(os.getenv("MATTERMOST_HTTP_TIMEOUT", "30"))


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def response_detail(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict):
        if "message" in payload:
            return str(payload["message"])
        return str(payload)
    return str(payload)


async def run() -> int:
    try:
        base_url = require_env("MATTERMOST_URL").rstrip("/")
        token = require_env("MATTERMOST_BOT_TOKEN")
        channel_id = require_env("MATTERMOST_TEST_CHANNEL_ID")
    except RuntimeError as error:
        print(f"FAIL configuration: {error}")
        return 1

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=f"{base_url}/api/v4",
        headers=headers,
        timeout=HTTP_TIMEOUT,
    ) as client:
        try:
            me = await client.get("/users/me")
            print(f"AUTH GET /api/v4/users/me -> {me.status_code}: {response_detail(me)}")
            if me.status_code != 200:
                return 1

            channel = await client.get(f"/channels/{channel_id}")
            print(
                f"CHANNEL GET /api/v4/channels/{channel_id} -> "
                f"{channel.status_code}: {response_detail(channel)}"
            )
            if channel.status_code != 200 or channel.json().get("id") != channel_id:
                return 1

            post = await client.post(
                "/posts",
                json={
                    "channel_id": channel_id,
                    "message": "SprintFlow Mattermost integration test ping",
                },
            )
            print(f"POST /api/v4/posts -> {post.status_code}: {response_detail(post)}")
            if post.status_code != 201 or not post.json().get("id"):
                return 1

            webhook_url = os.getenv("MATTERMOST_WEBHOOK_URL", "").strip()
            if webhook_url:
                webhook = await client.post(
                    webhook_url,
                    json={"text": "SprintFlow Mattermost webhook integration test ping"},
                )
                print(f"WEBHOOK POST {webhook_url} -> {webhook.status_code}: {response_detail(webhook)}")
                if webhook.status_code not in {200, 201}:
                    return 1
            else:
                print("WEBHOOK skipped: MATTERMOST_WEBHOOK_URL is not configured")
        except httpx.HTTPError as error:
            print(f"FAIL transport: {error}")
            return 1

    print("Mattermost integration test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
