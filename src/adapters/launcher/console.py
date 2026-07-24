import os
import sys
import shutil
from typing import Callable

def enable_ansi_windows():
    """Enable ANSI escape support in Windows Command Prompt (cmd.exe) using ctypes."""
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            hStdOut = kernel32.GetStdHandle(-11) # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(hStdOut, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(hStdOut, mode.value | 0x0004)
        except Exception:
            pass

def getch() -> str:
    """Cross-platform character reader that reads a single keypress without requiring Enter."""
    if os.name == 'nt':
        import msvcrt
        try:
            ch = msvcrt.getch()
            return ch.decode('utf-8', errors='ignore')
        except Exception:
            return ""
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        try:
            old_settings = termios.tcgetattr(fd)
        except Exception:
            # Fallback if stdin is not a tty (e.g. running in automated environment)
            return sys.stdin.read(1)
            
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch

def clear_screen():
    """Clear the terminal screen and reset cursor position."""
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()

def draw_header():
    """Draw interactive launcher title header."""
    columns, _ = shutil.get_terminal_size()
    print("=" * columns)
    title = "🚀 HYPERAGENT PROXY SERVER INTERACTIVE LAUNCHER"
    print(title.center(columns))
    print("=" * columns)

def draw_quick_start_guide(local_ip: str, global_ip: str, port: int, custom_key: str):
    """Draw the proxy guide dashboard in the terminal."""
    columns, _ = shutil.get_terminal_size()
    print("=" * columns)
    title = "🎉 HYPERAGENT LOCAL PROXY ONLINE 🎉"
    print(title.center(columns))
    print("=" * columns)
    print(f" 🏠 Local LAN IP:      http://{local_ip}:{port}")
    print(f" 🌐 Global IP:         http://{global_ip}:{port}")
    print(f" 🔌 Service Port:      {port} (Randomly allocated)")
    print(f" 🔑 Enforced API Key:  {custom_key if custom_key else 'None (Any key accepted)'}")
    print("=" * columns)
    print("\n 📖 QUICK START & REFERENCE GUIDE:")
    print("-" * columns)
    print("  1. CLIENT CONFIGURATION (OpenCode, Continue, or Python SDK):")
    print(f"     - Base URL:  http://localhost:{port}/v1")
    print("     - Provider:  @ai-sdk/openai-compatible")
    print(f"     - API Key:   {custom_key if custom_key else 'Any value is fine'}")
    print("\n  2. GETTING MODELS & SPECIFICATIONS:")
    print(f"     - Get all active models:  GET http://localhost:{port}/v1/models")
    print(f"     - Interactive API specs: GET http://localhost:{port}/docs")
    print("     - Cookies read: Automatically synced from browser!")
    print("\n  3. HOW TO CONFIGURE IN OPENCODE DESKTOP:")
    print("     - Open your ~/.config/opencode/opencode.jsonc file.")
    print("     - Find/add the \"hyperagent\" provider section block.")
    print(f"     - Set options.baseURL to \"http://localhost:{port}/v1\".")
    print(f"     - Set options.apiKey to \"{custom_key if custom_key else 'temp-key'}\".")
    print("-" * columns)
    msg = ">>> Press ANY KEY to dismiss this guide and show live logs <<<"
    print(f"\n{msg.center(columns)}")
    print("=" * columns)
    sys.stdout.flush()

def draw_split_screen(local_ip: str, global_ip: str, port: int, custom_key: str, log_buffer: list):
    """Draw a split screen containing dashboard header at the top and tailing logs below."""
    columns, _ = shutil.get_terminal_size()
    sys.stdout.write("\033[H")
    sys.stdout.write(
        f"🏠 Local IP: http://{local_ip}:{port}/v1 | 🌐 Global IP: http://{global_ip}:{port}/v1\n"
        f"🔑 Key: {custom_key if custom_key else 'None'} | 🔄 Press Ctrl+N to clear cookies and restart | Ctrl+C to stop\n"
        + "=" * columns + "\n"
    )
    for line in log_buffer:
        sys.stdout.write(line)
    sys.stdout.write("\033[J")
    sys.stdout.flush()
