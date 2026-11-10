import type { Metadata, Viewport } from "next";
import { Inter, Source_Serif_4 } from "next/font/google";
import "./globals.css";

// The tokens name Inter / Source Serif 4 but nothing loaded them, so
// browsers fell back to whatever matched (serif body copy in the audit's
// dark-mode screenshots). next/font self-hosts the files at build time:
// no request to Google at runtime, which is what PRIVACY.md promises.
const sans = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--oc-font-sans",
});
const serif = Source_Serif_4({
  subsets: ["latin"],
  display: "swap",
  variable: "--oc-font-serif",
  weight: ["400", "600"],
});

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
    <html lang="en" className={`${sans.variable} ${serif.variable}`}>
      <body className="min-h-screen font-sans antialiased">{children}</body>
    </html>
  );
}
