# Chat Archiver — security model

## Assets
| Asset | Where it lives |
|---|---|
| Telegram `api_id` / `api_hash` (owner's app) | Admin vault only (DPAPI). Approved devices receive them inside an activation code and keep them DPAPI-encrypted. **Never in any executable.** |
| Bot token | Admin vault only. Never in the distributed app. Redacted from Admin errors and logs. |
| Owner signing key (Ed25519) | Admin vault only; password-protected backup (scrypt + ChaCha20-Poly1305). The app contains only the public verify key. |
| Device key (X25519) | Generated on each device, DPAPI-encrypted, never leaves it. |
| User Telegram session | DPAPI-encrypted on the user's PC; visible in Telegram → Devices as “Chat Archiver”. |
| Exported chats | User's disk only. |

## Protocol
1. The device sends `/start a<device_pub><label>` to the owner's bot **from the user's own Telegram**
   (deep link + Start). Telegram vouches for the sender's identity.
2. On Approve, Admin issues an activation code: `{api_id, api_hash, user_id, issued_at, expires_at=+14d}`,
   encrypted to the device key (X25519 → HKDF-SHA256 → ChaCha20-Poly1305) and signed with Ed25519 over
   `prefix ‖ device_pub ‖ eph_pub ‖ ciphertext`. 217 characters, delivered in a `copy_text` button.
3. The app verifies the signature with the embedded verify key, decrypts with its device key, and stores the grant.
4. After sign-in the app checks: logged-in account = `user_id`; expiry against **Telegram server time**
   (`help.getConfig`); no newer signed revoke notice from the bot (read-only history read).

## Guarantees
- Stealing, unpacking or decompiling the exe yields no API keys, no bot token, no private key
  (verified by unpacking the built exe and searching every entry: `checks/check_exe_secrets.py`).
- A code is useless on another computer (encrypted to that device) or another account (user_id check).
- Codes cannot be forged or extended (Ed25519; server-time expiry).
- The Telegram client is wrapped in a request allowlist (`tg_export/guard.py`): it cannot send, edit, delete,
  react, mark as read or leave. Joining a group happens only after an explicit confirmation dialog.
- Dependencies pinned with SHA-256 hashes. The Flet desktop client archive is bundled at build time,
  so the app downloads nothing at runtime.

## Revocation: how strong it is
Revoke notices travel through the user's own chat with the bot, so revocation is **best effort**:
- The app reads every bot message since the grant was issued (user messages can't push it out of view)
  and **fails closed**: if the chat can't be read, access is not granted. It re-checks before every archive,
  not only at start-up.
- A user who deletes the revoke notice from their own chat history hides it from the app. The hard limit
  then is the 14-day expiry, checked against Telegram's server time.
- Revocation cannot take back API keys an approved user has already extracted.

## Residual risks (accepted)
- An **approved** user can extract `api_id`/`api_hash` from the memory of their running app.
  Mitigation: approve only trusted people; Admin keeps a history of who was approved when.
- Client-side checks (expiry, revoke) can be patched out of a modified exe — but a patched exe
  still has no API keys unless an approved user leaks them.
- No code signing: authenticity of the exe relies on direct delivery and the published SHA-256.
- Malware running under the user's Windows account can decrypt DPAPI data.
- Telegram may disable the owner's API app if users abuse it.
