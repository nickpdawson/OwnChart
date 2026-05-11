import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "OwnChart",
  description: "Patient-owned longitudinal health intelligence.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  // Two themeColors so iOS / Android browser chrome blends with our
  // surface in both color schemes.
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fafaf6" },
    { media: "(prefers-color-scheme: dark)", color: "#0e1014" },
  ],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
