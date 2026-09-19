"""Neutral public configuration loaded from environment variables."""

import os

from dotenv import load_dotenv


load_dotenv()


CHILDREN = {
    "CHILD_1": {
        "name": os.environ.get("CHILD_1_NAME", "Child 1"),
        "family_link_child_id": os.environ.get("CHILD_1_FAMILY_LINK_CHILD_ID", ""),
        "family_link_device_id": os.environ.get("CHILD_1_FAMILY_LINK_DEVICE_ID", ""),
        "reward_minutes": int(os.environ.get("CHILD_1_REWARD_MINUTES", "20")),
    },
    "CHILD_2": {
        "name": os.environ.get("CHILD_2_NAME", "Child 2"),
        "family_link_child_id": os.environ.get("CHILD_2_FAMILY_LINK_CHILD_ID", ""),
        "family_link_device_id": os.environ.get("CHILD_2_FAMILY_LINK_DEVICE_ID", ""),
        "reward_minutes": int(os.environ.get("CHILD_2_REWARD_MINUTES", "20")),
    },
}
