/** Local deployments keep identity and application data on this device. Only explicit false enables hosted accounts. */
export function isLocalMode(): boolean {
  return import.meta.env.VITE_AIRI_LOCAL_MODE !== 'false'
}

/** These routes depend on hosted identity or billing. Model-provider configuration remains available locally. */
export function isHostedRoute(path: string): boolean {
  return /^\/(?:auth(?:\/|$)|settings\/(?:account|flux)(?:\/|$))/.test(path)
}
