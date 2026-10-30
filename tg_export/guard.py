"""Read-only guard.

Every Telegram API request made by Telethon on behalf of the user goes through
``TelegramClient._call`` (including downloads from other data centres). We
override it and refuse anything that is not on the allowlist, so the program
cannot send, edit, delete, react, mark as read, leave or join a chat — even if
there were a bug elsewhere in the code.
"""
from __future__ import annotations

from telethon import TelegramClient, utils

# Requests the program needs. Names are "<namespace>.<ClassName>".
ALLOWED = frozenset({
    # connection and session state
    "help.GetConfigRequest",
    "help.GetCdnConfigRequest",
    "help.GetNearestDcRequest",
    "updates.GetStateRequest",
    "updates.GetDifferenceRequest",
    "updates.GetChannelDifferenceRequest",
    # login / logout
    "auth.SendCodeRequest",
    "auth.SignInRequest",
    "auth.CheckPasswordRequest",
    "account.GetPasswordRequest",
    "auth.ExportLoginTokenRequest",
    "auth.ImportLoginTokenRequest",
    "auth.ExportAuthorizationRequest",
    "auth.ImportAuthorizationRequest",
    "auth.LogOutRequest",
    # resolving chats and users
    "users.GetUsersRequest",
    "users.GetFullUserRequest",
    "contacts.ResolveUsernameRequest",
    "channels.GetChannelsRequest",
    "channels.GetFullChannelRequest",
    "messages.GetChatsRequest",
    "messages.GetFullChatRequest",
    "messages.GetDialogsRequest",
    "messages.GetPeerDialogsRequest",
    "messages.CheckChatInviteRequest",
    # reading history
    "messages.GetHistoryRequest",
    "messages.GetMessagesRequest",
    "channels.GetMessagesRequest",
    "messages.SearchRequest",
    "messages.GetRepliesRequest",
    # downloading files
    "upload.GetFileRequest",
    "upload.GetCdnFileRequest",
    "upload.ReuploadCdnFileRequest",
    "upload.GetCdnFileHashesRequest",
    "upload.GetFileHashesRequest",
})

# Allowed only when the user passes --join explicitly.
JOIN_REQUESTS = frozenset({"messages.ImportChatInviteRequest"})

# "Get*" requests are read-only by Telegram API convention and are allowed as a
# fallback so a Telethon update cannot break the program. These few have side
# effects and stay blocked.
DENIED_GETTERS = frozenset({
    "messages.GetMessagesViewsRequest",      # can increment view counters
    "messages.GetBotCallbackAnswerRequest",  # presses an inline bot button
    "messages.GetInlineBotResultsRequest",   # sends a query to a bot
    "contacts.GetLocatedRequest",            # can publish own geolocation
})


class BlockedRequestError(RuntimeError):
    """Raised when code tries to send a request outside the allowlist."""


def request_name(request) -> str:
    module = type(request).__module__.rsplit(".", 1)[-1]
    name = type(request).__name__
    return name if module == "functions" else f"{module}.{name}"


def check_request(request, allow_join: bool = False) -> None:
    name = request_name(request)
    if name in ALLOWED or (allow_join and name in JOIN_REQUESTS):
        return
    if name.split(".")[-1].startswith("Get") and name not in DENIED_GETTERS:
        return
    raise BlockedRequestError(
        f"Запрос {name} заблокирован: tg-export работает только на чтение"
        + (" (вступление в группу разрешает флаг --join)" if name in JOIN_REQUESTS else "")
    )


class ReadOnlyClient(TelegramClient):
    """TelegramClient that refuses every request outside the allowlist."""

    def __init__(self, *args, allow_join: bool = False, **kwargs):
        self._allow_join = allow_join
        super().__init__(*args, **kwargs)

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        for r in (request if utils.is_list_like(request) else [request]):
            check_request(r, self._allow_join)
        return await super()._call(sender, request, ordered=ordered,
                                   flood_sleep_threshold=flood_sleep_threshold)
