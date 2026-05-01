import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Mounted under /pdf_comp on the host. Must stay in sync with
  // `BASE_PATH` in lib/config.ts and the Caddy path matchers.
  basePath: "/pdf_comp",
  output: "standalone",
  experimental: {
    serverActions: {
      bodySizeLimit: "1100mb",
    },
  },
  async headers() {
    return [
      {
        // Tell every crawler to skip indexing this service. Combined with
        // robots.txt and the <meta name="robots"> tag in layout.tsx, even
        // crawlers that ignore robots.txt or skim only headers won't index.
        source: "/:path*",
        headers: [
          { key: "X-Robots-Tag", value: "noindex, nofollow, noarchive" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
