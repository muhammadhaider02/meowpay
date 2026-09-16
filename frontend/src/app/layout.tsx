import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MeowPay",
  description: "A digital wallet for cats. Humans top it up, cats send each other treats.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
