"""Watchdog — auto-restart bot on crash."""
import subprocess
import time
import sys
import os
import io

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Fix Windows encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RESTART_DELAY = 10
MAX_RESTARTS = 10
RESET_WINDOW = 300

restart_count = 0
start_time = time.time()


def notify(msg):
    try:
        import requests
        import config
        token = config.TELEGRAM_TOKEN
        chat = config.TELEGRAM_CHAT_ID
        if token and chat:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat, "text": msg}, timeout=10)
    except Exception:
        pass


def main():
    global restart_count, start_time

    print("[Watchdog] Starting bot...")
    notify("[Watchdog] Bot started")

    while True:
        try:
            proc = subprocess.Popen(
                [sys.executable, "main.py"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding='utf-8', errors='replace'
            )

            for line in proc.stdout:
                line = line.strip()
                if line:
                    print(f"[BOT] {line}")

            proc.wait()
            exit_code = proc.returncode
            runtime = time.time() - start_time

            print(f"[Watchdog] Bot crashed (exit={exit_code}, runtime={int(runtime)}s)")

            if runtime > RESET_WINDOW:
                restart_count = 0

            restart_count += 1

            if restart_count > MAX_RESTARTS:
                msg = (f"[Watchdog] Bot crashed {restart_count}x in a row. "
                       f"Auto-restart STOPPED.")
                print(msg)
                notify(msg)
                sys.exit(1)

            msg = (f"[Watchdog] Crash #{restart_count}/{MAX_RESTARTS}. "
                   f"Restart in {RESTART_DELAY}s...")
            print(msg)
            notify(msg)

            time.sleep(RESTART_DELAY)
            start_time = time.time()
            print(f"[Watchdog] Restart #{restart_count}...")

        except KeyboardInterrupt:
            print("[Watchdog] Stopped by user")
            break
        except Exception as e:
            print(f"[Watchdog] Error: {e}")
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
