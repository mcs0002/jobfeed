# Koch WAF cookie capture

Koch's Avature board uses an AWS WAF browser challenge. The scraper remains
plain HTTP, but it must replay the short-lived cookies produced by a real
browser. Without them, Koch fails loud and the rest of the scan continues.

1. Open the [scoped Koch Supply & Trading board](https://koch.avature.net/de_DE/careers/SearchJobs/?732=6319&732_format=1077&listFilterMode=1&jobRecordsPerPage=6&) in Chrome and let the job list load.
2. Open Developer Tools, select **Application → Storage → Cookies → `https://koch.avature.net`**.
3. Copy `secrets/koch_cookies.example.json` to
   `secrets/koch_cookies.json`. Replace the placeholders with the current
   `aws-waf-token`, `__cf_bm`, `ScustomPortal-*`, and `portalLanguage-*` cookie
   values shown in Developer Tools. Cookie names can vary; preserve their exact
   names. Delete example keys that are not present.
4. Set `user_agent` to the browser's exact user-agent string, available by
   entering `navigator.userAgent` in the Developer Tools Console. Set
   `captured_at` to today's date.
5. Verify only this source:

   ```bash
   .venv/bin/python -m jobfeed --verify --company "Koch Supply & Trading"
   ```

`secrets/koch_cookies.json` is gitignored. Never commit or share it. When the
source reports `KOCH_COOKIES_EXPIRED`, repeat the capture; the other sources and
stored Koch rows remain unaffected in the meantime.
