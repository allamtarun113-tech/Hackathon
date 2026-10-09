import { NextResponse, type NextRequest } from "next/server";

/**
 * Per-request nonce Content-Security-Policy. Next.js reads the nonce from the request's CSP
 * header and applies it to its own scripts, so neither scripts nor styles need 'unsafe-inline'.
 */
export function middleware(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const apiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
  const isDev = process.env.NODE_ENV === "development";

  const policy = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    `connect-src 'self' ${apiOrigin} https://*.ingest.sentry.io https://*.ingest.us.sentry.io`,
    "img-src 'self' data:",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...(isDev ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", policy);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  matcher: [{ source: "/((?!_next/static|_next/image|favicon.ico).*)" }],
};
