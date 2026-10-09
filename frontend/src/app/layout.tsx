import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "PagerMind",
  description: "On-call incident assistant with Hindsight memory.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Reading the request headers opts every page into dynamic rendering, which per-request CSP nonces require.
  await headers();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
