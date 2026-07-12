# Email — set up firm mail on your phone

1. Enrol the phone in mobile device management first: install the Firm MDM app from your app store and sign in with SSO. Company mail requires a managed device.
2. Approve the MFA push when prompted, then let the profile finish installing (you may see a "Remote Management" prompt on iOS — accept it).
3. Open the built-in Mail app. Add an "Exchange" (iOS) or "Corporate" (Android) account using your full firm email address.
4. When asked for a server, enter `mail.firm.internal`; leave the username as your email address and authenticate through the SSO page that appears.
5. If mail does not sync: confirm the device shows as "Compliant" in the MDM app — a non-compliant device is blocked from mail until you resolve the flagged item (usually an OS update or passcode policy).
6. Still failing after the device is compliant: escalate with the device model, OS version, and the exact error.
