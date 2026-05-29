# OpenVPN GUI

A small Linux GUI for importing OpenVPN profiles and connecting or disconnecting them.

## Requirements

- Python 3 with Tkinter
- `openvpn`
- `pkexec` from Polkit, or `sudo`, so OpenVPN can create the VPN tunnel

Ubuntu/Debian example:

```bash
sudo apt install python3-tk openvpn policykit-1
```

## Install

```bash
sudo dpkg -i openvpngui_1.0.0_all.deb
```

## Use

1. Click **Import** and choose a `.ovpn` or `.conf` file.
2. Give the profile a friendly name.
3. Select the profile and click **Connect**.
4. Click **Disconnect** to stop the active tunnel.

Imported profiles are saved in:

```text
~/.config/openvpngui/profiles
```

The app also copies common relative companion files referenced by the profile, such as `ca`, `cert`, `key`, `tls-auth`, `tls-crypt`, `pkcs12`, and `auth-user-pass`. Profiles that reference absolute file paths still need those paths to remain available.

## Notes

- Some OpenVPN profiles prompt for username/password in a terminal. For a GUI flow, use a profile with an `auth-user-pass` file or embedded provider-specific authentication.
- Closing the window while connected asks whether to disconnect first.
