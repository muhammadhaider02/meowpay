import type { NextConfig } from "next";

// Deliberately bare. There is no rewrite proxying /api to the backend, because
// the browser must talk to FastAPI directly for CORS to be the thing that
// authorises the origin. A same-origin rewrite would hide a misconfigured
// CORS_ORIGINS until someone opened the deployed app from a second domain.
const nextConfig: NextConfig = {
  reactStrictMode: true,
};

export default nextConfig;
