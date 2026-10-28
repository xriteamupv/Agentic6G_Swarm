import type { Metadata } from "next";
import "./globals.css";
import { Inter } from "next/font/google";
import React from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { APP_NAME } from "@/lib/branding";

const inter = Inter({
  subsets: ["latin"],
  preload: true,
  display: "swap",
});

export const metadata: Metadata = {
  title: APP_NAME,
  description: "Chat interface for swarm robotics agents",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className="dark"
      style={{ colorScheme: "dark" }}
    >
      <body className={inter.className}>
        <NuqsAdapter>{children}</NuqsAdapter>
      </body>
    </html>
  );
}
