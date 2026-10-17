# Frontend dependency review

Production dependencies have no known advisories in the release audit. Run
`npm audit --omit=dev` to refresh that result; advisory databases change.
`npm run test:security` exercises the installed HTTP client and parsing APIs.
The normal build runs this gate before generating assets.

The development dependency chain Tailwind CSS 3 → glob/watch tooling → braces
still carries [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm).
No patched braces release was available at the 2026-10-03 review. These tools
run during builds against repository-controlled patterns; braces is not shipped
as a production dependency or used to process platform users' requests.
Only build reviewed source in trusted environments. Do not expose the development
server or feed untrusted glob patterns into build/watch tooling. A Tailwind 4
migration needs separate stylesheet and visual regression validation; do not
hide this finding with an audit ignore or an incompatible forced downgrade.
