"""PyInstaller entry point for Chat Archiver Admin."""
import runpy

runpy.run_module("chat_archiver_admin", run_name="__main__", alter_sys=True)
