import subprocess

import pytest

FILES = {
    "app/db.py": (
        "from sqlalchemy.orm import relationship\n"
        "def get_db():\n"
        "    yield None\n"
        "class Customer:\n"
        "    pass\n"
        "class Order:\n"
        '    customer = relationship("Customer")\n'
    ),
    "app/api.py": (
        "from fastapi import APIRouter, Depends\n"
        "from app.db import get_db\n"
        "router = APIRouter()\n"
        '@router.get("/orders")\n'
        "def list_orders(db=Depends(get_db)):\n"
        "    return []\n"
        "def get():\n"
        "    return 1\n"
    ),
    "app/other.py": "def get():\n    return 2\n",
    "app/graph.py": (
        "from langgraph.graph import StateGraph\n"
        "def classify(s):\n"
        "    return s\n"
        "def handle(s):\n"
        "    return s\n"
        "def route_fn(s):\n"
        '    return "handle"\n'
        "g = StateGraph(dict)\n"
        'g.add_node("classify", classify)\n'
        'g.add_node("handle", handle)\n'
        'g.add_edge("classify", "handle")\n'
        'g.add_conditional_edges("handle", route_fn)\n'
    ),
    "app/tasks.py": (
        "from celery import shared_task\n"
        "@shared_task\n"
        "def send_mail(to):\n"
        "    return to\n"
        "def notify():\n"
        '    send_mail.delay("x")\n'
    ),
    "app/unused.py": "def orphan():\n    return None\n",
    "web/index.ts": (
        "export function helper(): number { return 1; }\n"
        "export function main(): number { return helper(); }\n"
    ),
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    for a in (
        ["init", "-q"],
        ["add", "-A"],
        [
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
            "--no-verify",
        ],
    ):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path
