# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

```bash
# Run directly (development)
python abrir_caja.py

# Install dependencies
pip install -r requirements.txt
```

## Building the Executable

```bash
# Build with PyInstaller (outputs AbrirCajon.exe in dist/)
pyinstaller abrir_caja.spec
```

## Architecture

This is a single-file Windows desktop application (`abrir_caja.py`) that combines:

- **FastAPI HTTP server** — runs on port `8444` in a background `threading.Thread`. Exposes endpoints to open a cash drawer via ESC/POS printer command.
- **PySide6 GUI** — `MainWindow` lets the user configure username, PIN, and printer. The app lives in the system tray (`QSystemTrayIcon`) and does not close when the window is hidden.
- **Cloudflare Tunnel** — `cloudflared` is launched as a subprocess to expose the local HTTP server publicly. The tunnel URL is automatically sent to `https://adm.premium-soft.com/Configuracion/GuardarUrlCloudflare`.

### Key globals
- `servidor_thread` — the uvicorn server thread
- `cloudflared_process` — the `cloudflared` subprocess
- `cloudflared_url` — the extracted `trycloudflare.com` URL

### Configuration
Persisted in `config.json` (same directory as the executable):
```json
{ "usuario": "...", "pin": "...", "impresora": "..." }
```
When frozen with PyInstaller, `DIRECTORIO_APLICACION` points to the directory of the `.exe`; in development it points to the script's directory.

### API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Server info + status |
| GET | `/status` | Status + config summary |
| GET | `/health` | Health check |
| GET | `/test` | Connectivity test |
| GET | `/impresoras` | List Windows printers |
| GET | `/url-tunel` | Current Cloudflare URL |
| POST | `/abrir-caja` | Open cash drawer (`usuario` + optional `pin`) |

### Cash drawer command
`bytes([27, 112, 0, 64, 65])` — standard ESC/POS drawer-kick sequence sent via `win32print`.

## Platform Notes

- **Windows only** — uses `win32print` (pywin32) for printer access.
- Requires `cloudflared` to be installed and available on `PATH` for the tunnel feature.
- Logs are written to `servidor.log` in the application directory.
