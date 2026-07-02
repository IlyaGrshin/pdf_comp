import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Mounted under /pdf_comp on the host. Must stay in sync with
  // `BASE_PATH` in lib/config.ts and the Caddy path matchers.
  basePath: "/pdf_comp",
  output: "standalone",
  poweredByHeader: false,
  // Every request is a fresh compression job — there is nothing to
  // cache. Disable the default 50 MB in-memory ISR/data cache so the
  // Node process doesn't hold that block for a purpose we never use.
  cacheMaxMemorySize: 0,
  // We don't render any `next/image`. Turning the optimizer off keeps
  // sharp from being lazy-loaded on a stray request and prevents its
  // per-request scratch buffer from being warmed.
  images: { unoptimized: true },
  experimental: {
    // Load route entries on first request instead of at server boot.
    // Idle RSS shrinks by whatever the API route + Python-spawn glue
    // pulls in — small in absolute MB, but the app has one active
    // route and this is pure win on a memory-tight VPS.
    preloadEntriesOnStart: false,
    // Reduces Webpack peak RAM during `next build`. Marginally slower
    // build; matters on the 2 GB Alwyzon host where the build step
    // was the tightest moment.
    webpackMemoryOptimizations: true,
    serverActions: {
      bodySizeLimit: "1100mb",
    },
  },
  async headers() {
    const isProd = process.env.NODE_ENV === "production";
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
              `script-src 'self' 'unsafe-inline'${isProd ? "" : " 'unsafe-eval'"}`,
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data:",
              "font-src 'self' data:",
              "connect-src 'self'",
              "frame-ancestors 'none'",
              "form-action 'self'",
              "base-uri 'self'",
              "object-src 'none'",
              // Skip in dev — would upgrade http://localhost to https:// and
              // break every subresource on the local dev server.
              ...(isProd ? ["upgrade-insecure-requests"] : []),
            ].join("; "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
