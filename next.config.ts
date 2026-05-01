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
        source: "/:path*",
        headers: [
          // Crawlers — don't index. Layered with robots.txt + <meta name=robots>.
          { key: "X-Robots-Tag", value: "noindex, nofollow, noarchive" },
          // Don't leak the visited URL when users click outbound links.
          { key: "Referrer-Policy", value: "no-referrer" },
          // Prevent MIME-sniffing attacks where a browser interprets a file
          // differently from its declared Content-Type.
          { key: "X-Content-Type-Options", value: "nosniff" },
          // Prevent clickjacking. CSP `frame-ancestors 'none'` does the same
          // but X-Frame-Options is honored by older browsers too.
          { key: "X-Frame-Options", value: "DENY" },
          // Disable browser features the app doesn't use.
          {
            key: "Permissions-Policy",
            value: "geolocation=(), microphone=(), camera=(), interest-cohort=()",
          },
          // Defense-in-depth CSP. `'unsafe-inline'` on script/style stays
          // because Next.js's hydration scripts and Tailwind's runtime style
          // injection rely on it; switching to nonce-based would require a
          // middleware layer. Everything else is locked down.
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data:",
              "font-src 'self' data:",
              "connect-src 'self'",
              "frame-ancestors 'none'",
              "form-action 'self'",
              "base-uri 'self'",
              "object-src 'none'",
              "upgrade-insecure-requests",
            ].join("; "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
