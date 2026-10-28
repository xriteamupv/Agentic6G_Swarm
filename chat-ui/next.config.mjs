/** @type {import('next').NextConfig} */
const nextConfig = {
  // Production: a self-contained server (node .next/standalone/server.js), see
  // a6g-swarm deploy/ui/. `next dev` is for UI development only.
  output: "standalone",
  experimental: {
    serverActions: {
      bodySizeLimit: "10mb",
    },
  },
};

export default nextConfig;
