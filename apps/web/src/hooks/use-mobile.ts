import * as React from "react"

const MOBILE_BREAKPOINT = 768
const QUERY = `(max-width: ${MOBILE_BREAKPOINT - 1}px)`

function subscribe(onChange: () => void) {
  const mql = window.matchMedia(QUERY)
  mql.addEventListener("change", onChange)
  return () => mql.removeEventListener("change", onChange)
}

/** True below the mobile breakpoint. Subscribes to the media query (React 19: no setState inside an effect);
 * renders as desktop on the server. */
export function useIsMobile() {
  return React.useSyncExternalStore(subscribe, () => window.matchMedia(QUERY).matches, () => false)
}
