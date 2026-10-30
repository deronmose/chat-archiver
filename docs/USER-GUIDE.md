# Chat Archiver — quick guide

Chat Archiver saves the full history of Telegram groups and channels you are a member of:
a readable web page (`chat.html`), a spreadsheet (`messages.csv`), `messages.json`, and photos/files.
It is **read-only**: it never sends messages, deletes anything or marks chats as read.

Chat Archiver is an independent app that uses the Telegram API. It is not affiliated with Telegram.

## 1. Start the app

Double-click **Chat Archiver.exe**. No installation is needed.

> Windows may show **“Windows protected your PC”** because the app is not code-signed.
> Click **More info → Run anyway**. Only do this for a copy you received directly from
> the person who shared it with you.

## 2. Request access (first time only)

1. Click **Request access**. Your Telegram app opens the owner's bot
   (or scan the QR code with your phone).
2. Press **Start** in the bot chat. The owner sees your Telegram name and your computer's name.
3. Wait for the owner's approval. The bot sends you a **Copy activation code** button — tap it.
4. Come back to Chat Archiver. It picks the code up automatically.
   (If it doesn't, paste the code into the field and click **Activate**.)

## 3. Sign in to Telegram

Scan the QR code: on your phone open **Telegram → Settings → Devices → Link Desktop Device**.
Or switch to **Phone number** and enter the code Telegram sends you.
If your account has a two-step verification password, you will be asked for it.

## 4. Archive a chat

1. Paste a link to the group or channel (`t.me/name`, `t.me/+invite`, a message link) and click **Find** —
   or pick it from the list.
2. If the group has **topics**, choose **All topics** or one topic. A link to a message inside
   a topic selects that topic automatically.
3. Click **Download**.

When it's done, click **Open chat** to read it in your browser, or **Open folder**.
By default archives are saved to `Documents\Chat Archiver`. Change this under **Options**,
along with media download, the file size limit, a start date and the file formats.

**Stopped or lost connection?** Just run it again — it continues where it stopped
and only downloads what's new.

## Good to know

- Archives contain other people's messages. Keep them private and don't use them to train AI models.
- Some chats restrict saving content. Chat Archiver shows a warning for them — please respect the admins' wishes.
- To join a group from an invite link, Chat Archiver asks you first.
- Your sign-in is stored encrypted on this computer. To sign out: **⋮ → Log out**.
  The app is also listed in Telegram → Settings → Devices as “Chat Archiver”.
