# Device setup — new laptop first boot

1. Power on connected to any network and sign in with your SSO credentials — enrollment starts automatically.
2. Wait for the management profile to finish (status bar in the enrollment window; typically 15–30 minutes). Do not reboot during this step.
3. Install baseline apps from Self Service: Slack, browser, VPN client.
4. Enroll MFA on the new device at `https://mfa.firm.internal` while your old device is still available for verification.
5. Confirm disk encryption is on (Settings → Privacy & Security → FileVault / BitLocker shows "on"). If it is off after enrollment, escalate.
6. Old device: hand it back to IT within 14 days — data is wiped on return.
