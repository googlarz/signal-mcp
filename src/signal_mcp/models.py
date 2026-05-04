"""Data models for Signal messages, contacts, and groups."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Attachment:
    content_type: str
    filename: str
    local_path: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    caption: str | None = None


@dataclass
class Message:
    id: str
    sender: str
    body: str
    timestamp: datetime
    attachments: list[Attachment] = field(default_factory=list)
    group_id: str | None = None
    recipient: str | None = None  # set for outgoing DMs
    quote_id: str | None = None
    reactions: dict[str, str] = field(default_factory=dict)  # emoji -> sender
    is_read: bool = False
    receipt_type: str | None = None  # "DELIVERY" or "READ" for receipt envelopes
    expires_in_seconds: int | None = None  # disappearing message timer
    view_once: bool = False            # view-once message
    is_reaction: bool = False          # this is a reaction message (not stored)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "body": self.body,
            "timestamp": self.timestamp.isoformat(),
            "attachments": [
                {
                    "content_type": a.content_type,
                    "filename": a.filename,
                    "local_path": a.local_path,
                    "size": a.size,
                    "width": a.width,
                    "height": a.height,
                    "caption": a.caption,
                }
                for a in self.attachments
            ],
            "group_id": self.group_id,
            "quote_id": self.quote_id,
            "reactions": self.reactions,
            "is_read": self.is_read,
            "receipt_type": self.receipt_type,
            "expires_in_seconds": self.expires_in_seconds,
            "view_once": self.view_once,
        }


@dataclass
class Contact:
    number: str
    uuid: str | None = None
    name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    profile_name: str | None = None
    about: str | None = None
    blocked: bool = False

    @property
    def display_name(self) -> str:
        # Prefer explicit name set by user, then profile full name, then number
        if self.name and self.name.strip():
            return self.name.strip()
        parts = " ".join(filter(None, [self.given_name, self.family_name])).strip()
        if parts:
            return parts
        return self.profile_name or self.number or ""

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "uuid": self.uuid,
            "name": self.name,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "profile_name": self.profile_name,
            "about": self.about,
            "blocked": self.blocked,
            "display_name": self.display_name,
        }


@dataclass
class GroupMember:
    uuid: str
    number: str | None = None
    is_admin: bool = False


@dataclass
class Group:
    id: str
    name: str = ""
    members: list[GroupMember] = field(default_factory=list)
    description: str | None = None
    is_blocked: bool = False
    is_member: bool = True
    admins: list[str] = field(default_factory=list)  # uuids
    invite_link: str | None = None

    @property
    def member_count(self) -> int:
        return len(self.members)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "member_count": self.member_count,
            "members": [
                {"uuid": m.uuid, "number": m.number, "is_admin": m.is_admin}
                for m in self.members
            ],
            "is_blocked": self.is_blocked,
            "is_member": self.is_member,
            "invite_link": self.invite_link,
        }


@dataclass
class SendResult:
    timestamp: int
    recipient: str
    success: bool
    error: str | None = None
