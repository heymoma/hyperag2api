import os
import sys
import time
import queue
import threading
from collections import deque
from typing import List

from src.services.auth_service import AuthService
from src.adapters.launcher.processes import ProcessManager
from src.adapters.launcher.system import (
    BrowserInfo,
    discover_browsers,
    platform_label,
    get_local_ip,
    get_global_ip,
    get_random_port,
)
from src.adapters.launcher.console import (
    enable_ansi_windows,
    getch,
    clear_screen,
    draw_header,
    draw_quick_start_guide,
    draw_split_screen,
)
from src.core.logging_config import setup_logging, get_logger

logger = get_logger("launcher")


def log_reader_worker(pipe, log_q: queue.Queue):
    """Worker thread that pipes stdout from the FastAPI process to the UI queue."""
    try:
        for line in iter(pipe.readline, ''):
            log_q.put(line)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def key_listener_worker(key_q: queue.Queue):
    """Worker thread that reads single keypresses and puts them into the queue."""
    while True:
        try:
            ch = getch()
            key_q.put(ch)
        except Exception:
            break


class LauncherOrchestrator:
    """Orchestrates the entire interactive console launcher session."""

    def __init__(self, auth_service: AuthService, process_manager: ProcessManager):
        self.auth_service = auth_service
        self.process_manager = process_manager
        self.should_clear_cookies = False
        self.custom_api_key = ""
        self.selected_browser: BrowserInfo = None  # type: ignore[assignment]
        self.wait_seconds = 45

    # -- helpers --------------------------------------------------------------
    def _prompt_browser(self, browsers: List[BrowserInfo]) -> BrowserInfo:
        """Render the detected-browser menu and return the user's choice."""
        print("\n🌐 Detected Browsers:")
        for idx, b in enumerate(browsers, 1):
            tags = [b.source]
            if not b.is_chromium:
                tags.append("experimental CDP")
            print(f"  [{idx}] {b.name}  \033[90m({', '.join(tags)})\033[0m")

        try:
            choice = input(f"\nSelect browser number [1-{len(browsers)}] (default: 1): ").strip()
            if not choice:
                return browsers[0]
            idx = int(choice) - 1
            if 0 <= idx < len(browsers):
                return browsers[idx]
            print("Invalid option. Defaulting to first choice.")
            return browsers[0]
        except (ValueError, IndexError):
            return browsers[0]
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            sys.exit(0)

    # -- main loop ------------------------------------------------------------
    def run(self):
        """Execute the primary control loop of the interactive CLI launcher."""
        setup_logging()
        enable_ansi_windows()
        clear_screen()
        draw_header()

        # Platform info
        print(f" [System Detected: {platform_label()}]")
        print("\033[91m⚠️  REQUIREMENT: You need a Chromium-based browser")
        print("   (Microsoft Edge, Google Chrome, Brave, Chromium) OR a")
        print("   Playwright-managed Chromium to capture session cookies.\033[0m")
        print("------------------------------------------------------------")

        # Quota/cookie clear prompt
        try:
            print("\033[93m🔄 Account / Quota Switch:\033[0m")
            print("Did you finish your model quota or want to change accounts?")
            clear_input = input("Would you like to clear cookies to re-authenticate? (y/N): ").strip().lower()
            if clear_input in ['y', 'yes']:
                self.should_clear_cookies = True
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            sys.exit(0)

        # API Key Prompt
        try:
            print("\n------------------------------------------------------------")
            print("\033[94m🔒 Proxy Security & Authentication:\033[0m")
            print("Would you like to lock down this local proxy with a custom API key?")
            self.custom_api_key = input("Set enforced API Key (Press Enter to allow any key to connect): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            sys.exit(0)

        # Autodiscover browsers (system + Playwright)
        browsers = discover_browsers()
        if not browsers:
            print("\n❌ Error: No supported browsers found.")
            print("   Install a Chromium-based browser (Edge, Chrome, Brave, Chromium),")
            print("   or install a Playwright Chromium with:  playwright install chromium")
            sys.exit(1)

        self.selected_browser = self._prompt_browser(browsers)
        print(f"Using browser: {self.selected_browser.name} "
              f"({self.selected_browser.source})")
        if not self.selected_browser.is_chromium:
            print("\033[93m⚠️  WARNING: This browser's CDP support is experimental. "
                  "Chromium-based browsers are recommended for correct cookie sync.\033[0m")

        # Session Loop
        while True:
            try:
                print("\n------------------------------------------------------------")
                print("\033[91m(Tip: If you are already logged in to Hyperagent, enter 0 or 1 to skip the wait)\033[0m")
                user_input = input("How many seconds should I wait for you to log in? (default: 45): ").strip()
                if not user_input:
                    self.wait_seconds = 45
                else:
                    self.wait_seconds = int(user_input)
            except ValueError:
                print("Invalid number. Defaulting to 45 seconds.")
                self.wait_seconds = 45
            except (KeyboardInterrupt, EOFError):
                print("\nExiting.")
                sys.exit(0)

            # Launch browser
            name = self.selected_browser.name
            print(f"\n[1/3] Launching {name} with remote debugging...")
            active = self.process_manager.launch_browser(
                self.selected_browser.command, self.selected_browser.family
            )
            if not active:
                print("❌ Failed to launch browser process.")
                sys.exit(1)
            print(f"✅ {name} is active.")

            # Clear cookies via auth service
            if self.should_clear_cookies:
                print("\n[CDP] Contacting browser context to clear session cookies...")
                self.auth_service.reset_authentication()
                self.should_clear_cookies = False

            # Countdown Wait
            print(f"\n[2/3] Waiting {self.wait_seconds} seconds for you to log in to https://hyperagent.com ...")
            print(f"Please use the {name} window that just opened.")
            print("------------------------------------------------------------")

            for remaining in range(self.wait_seconds, 0, -1):
                sys.stdout.write(f"\r⏳ Starting API services in {remaining} seconds... ")
                sys.stdout.flush()
                time.sleep(1)
            print("\r⏳ Time's up! Starting API services now...             \n")

            # Networking and Ports
            print("Retrieving network details...")
            local_ip = get_local_ip()
            global_ip = get_global_ip()
            port = get_random_port()

            # Start proxy server (bind 0.0.0.0 so LAN/global IPs are reachable)
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_vars = {"HOST": "0.0.0.0"}
            if self.custom_api_key:
                env_vars["PROXY_API_KEY"] = self.custom_api_key

            proc = self.process_manager.start_proxy_server(
                port, self.custom_api_key, app_dir, env_vars
            )

            # Pipe logs
            log_q: queue.Queue = queue.Queue()
            t_log = threading.Thread(target=log_reader_worker, args=(proc.stdout, log_q), daemon=True)
            t_log.start()

            time.sleep(1.5)

            # Show Guide Dashboard
            clear_screen()
            draw_quick_start_guide(local_ip, global_ip, port, self.custom_api_key)

            # Wait for any keypress to start showing logs
            try:
                getch()
            except (KeyboardInterrupt, EOFError):
                pass

            # Start keyboard listener
            key_q: queue.Queue = queue.Queue()
            t_key = threading.Thread(target=key_listener_worker, args=(key_q,), daemon=True)
            t_key.start()

            log_buffer: deque = deque(maxlen=20)
            restart_triggered = False

            draw_split_screen(local_ip, global_ip, port, self.custom_api_key, log_buffer)

            # Tailing Logs & Controls loop
            try:
                while True:
                    # Keyboard checks
                    try:
                        ch = key_q.get_nowait()
                        if ch == '\x0e':  # Ctrl+N
                            restart_triggered = True
                            break
                        elif ch == '\x03':  # Ctrl+C
                            raise KeyboardInterrupt()
                    except queue.Empty:
                        pass

                    # Log checks
                    try:
                        line = log_q.get_nowait()
                        log_buffer.append(line)
                        draw_split_screen(local_ip, global_ip, port, self.custom_api_key, log_buffer)
                    except queue.Empty:
                        pass

                    # Process status checks
                    if proc.poll() is not None:
                        # Append any leftover logs
                        while not log_q.empty():
                            log_buffer.append(log_q.get())
                        draw_split_screen(local_ip, global_ip, port, self.custom_api_key, log_buffer)
                        break

                    time.sleep(0.02)
            except KeyboardInterrupt:
                print("\nStopping proxy server...")
                self.process_manager.terminate_all()
                sys.exit(0)

            if restart_triggered:
                print("\n🔄 Ctrl+N detected! Stopping API proxy server...")
                self.process_manager.terminate_proxy_server()
                self.should_clear_cookies = True
                clear_screen()
                print("============================================================")
                print("            🔄 COOKIES CLEARED & RESTARTING FLOW             ")
                print("============================================================")
                # Loop back to the top and relaunch.
                continue

            # Otherwise the proxy exited on its own (crash or manual stop).
            exit_code = proc.poll()
            print(f"\n\033[91m⚠️  The proxy server process has stopped (exit code {exit_code}).\033[0m")
            try:
                again = input("Restart the proxy server? (Y/n): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                again = "n"
            if again in ("n", "no"):
                print("Exiting.")
                self.process_manager.terminate_all()
                sys.exit(0)
            clear_screen()
            # Loop back to the top and relaunch.
