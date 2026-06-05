import os
import re
import json
import time
from datetime import datetime, timezone
from collections import defaultdict
from atproto import Client

print("=== MILFBLEUSKY BOT STARTED ===")

LIST_URL = "https://bsky.app/profile/did:plc:mbmrdjswath6qc3sdpal5vqh/lists/3mfzoqcr7g62h"
NEWEST_LIST_URL = "https://bsky.app/profile/did:plc:mbmrdjswath6qc3sdpal5vqh/lists/3mmjli3y4nx2s"

MAX_PER_RUN = 100
MAX_PER_USER = 3
HOURS_BACK = 3

AUTHOR_POSTS_PER_MEMBER = 50
LIST_MEMBER_LIMIT = 1500
SLEEP_SECONDS = 2

STATE_FILE = os.getenv("STATE_FILE", "state_milfbleusky.json")
LIST_RE = re.compile(r"bsky\.app/profile/([^/]+)/lists/([^/?#]+)")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"reposted": {}, "liked": {}}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {"reposted": {}, "liked": {}}

        data.setdefault("reposted", {})
        data.setdefault("liked", {})
        return data

    except Exception:
        return {"reposted": {}, "liked": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def parse_list_uri(url):
    m = LIST_RE.search(url)
    if not m:
        raise ValueError("Ongeldige lijst URL")

    did_or_handle, rkey = m.group(1), m.group(2)
    return f"at://{did_or_handle}/app.bsky.graph.list/{rkey}"


def get_list_members(client, list_uri):
    members = []
    cursor = None

    while len(members) < LIST_MEMBER_LIMIT:
        params = {
            "list": list_uri,
            "limit": 100
        }

        if cursor:
            params["cursor"] = cursor

        resp = client.app.bsky.graph.get_list(params)

        for item in resp.items:
            members.append(item.subject.did)

            if len(members) >= LIST_MEMBER_LIMIT:
                break

        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break

    return members


def is_repost_item(item):
    return getattr(item, "reason", None) is not None


def is_reply(post):
    return bool(getattr(post.record, "reply", None))


def is_quote(post):
    embed = getattr(post.record, "embed", None)
    if not embed:
        return False

    text = str(embed).lower()
    return "app.bsky.embed.record" in text or "recordwithmedia" in text


def has_media(post):
    embed = getattr(post.record, "embed", None)
    if not embed:
        return False

    text = str(embed).lower()

    if "recordwithmedia" in text:
        return False

    return "images" in text or "video" in text


def is_valid_media_post(post):
    if not has_media(post):
        return False
    if is_reply(post):
        return False
    if is_quote(post):
        return False

    return True


def post_created_at(post):
    try:
        return post.record.created_at
    except Exception:
        return ""


def is_within_hours(created, hours):
    if not created:
        return False

    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        age_hours = (
            datetime.now(timezone.utc) - created_dt
        ).total_seconds() / 3600

        return age_hours <= hours

    except Exception:
        return False


def main():
    username = os.getenv("BSKY_USERNAME")
    password = os.getenv("BSKY_PASSWORD")

    if not username or not password:
        raise RuntimeError("BSKY_USERNAME of BSKY_PASSWORD ontbreekt")

    state = load_state()

    client = Client()
    client.login(username, password)

    print("Login OK")

    list_uri = parse_list_uri(LIST_URL)
    newest_list_uri = parse_list_uri(NEWEST_LIST_URL)

    members_main = get_list_members(client, list_uri)
    members_newest = get_list_members(client, newest_list_uri)

    members = list(set(members_main + members_newest))

    print(f"Hoofdlijst leden: {len(members_main)}")
    print(f"Newest lijst leden: {len(members_newest)}")
    print(f"Totaal unieke leden: {len(members)}")

    candidates = []
    per_user_seen = defaultdict(int)

    for did in members:
        try:
            feed = client.app.bsky.feed.get_author_feed({
                "actor": did,
                "limit": AUTHOR_POSTS_PER_MEMBER,
                "filter": "posts_with_replies"
            })

            for item in feed.feed:
                # Geen reposts
                if is_repost_item(item):
                    continue

                post = item.post

                uri = post.uri
                cid = post.cid
                author_did = post.author.did
                created = post_created_at(post)

                # Alleen originele posts van dit lijstlid
                if author_did != did:
                    continue

                if uri in state["reposted"]:
                    continue

                # Alleen media, geen reply, geen quote
                if not is_valid_media_post(post):
                    continue

                if not is_within_hours(created, HOURS_BACK):
                    continue

                candidates.append({
                    "uri": uri,
                    "cid": cid,
                    "author": author_did,
                    "created_at": created
                })

        except Exception as e:
            print(f"Skip member {did}: {e}")

    # Oudste eerst, nieuwste als laatste
    candidates.sort(key=lambda x: x["created_at"])

    print(f"Originele mediapost kandidaten laatste {HOURS_BACK} uur: {len(candidates)}")

    done = 0

    for item in candidates:
        if done >= MAX_PER_RUN:
            break

        author = item["author"]

        if per_user_seen[author] >= MAX_PER_USER:
            continue

        uri = item["uri"]
        cid = item["cid"]

        try:
            client.repost(uri, cid)

            state["reposted"][uri] = {
                "cid": cid,
                "author": author,
                "time": now_iso()
            }

            print(f"Reposted: {uri}")
            time.sleep(SLEEP_SECONDS)

            try:
                client.like(uri, cid)

                state["liked"][uri] = {
                    "cid": cid,
                    "time": now_iso()
                }

                print(f"Liked: {uri}")

            except Exception as e:
                print(f"Like fout: {e}")

            per_user_seen[author] += 1
            done += 1

            save_state(state)
            time.sleep(SLEEP_SECONDS)

        except Exception as e:
            print(f"Repost fout: {e}")

    save_state(state)
    print(f"Klaar. Gerepost: {done}")


if __name__ == "__main__":
    main()