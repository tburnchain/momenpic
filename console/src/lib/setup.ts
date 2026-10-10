/** Storage keys for the owner auto-redirect to /setup when readiness checks fail. */
export const SETUP_DISMISS_KEY = "acvm.setup.redirectDismissed"; // localStorage: never redirect again
export const SETUP_DONE_KEY = "acvm.setup.redirected"; // sessionStorage: once per browser session
export const SETUP_NOTICE_KEY = "acvm.setup.notice"; // sessionStorage: show the "why am I here" banner
