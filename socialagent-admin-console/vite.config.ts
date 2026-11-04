// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  // Vite 7 drops /*! */ and @license comments from the bundle by default. Keep
  // them inline, as Vite 6 did, so third-party license notices still ship.
  esbuild: { legalComments: "inline" },
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    target: "es2022",
    chunkSizeWarningLimit: 800,
    // Emit no inline module-preload polyfill so the built index.html carries no
    // inline <script>. This lets the CSP drop `script-src 'unsafe-inline'`.
    // Native modulepreload is supported by the es2022 target's browser baseline.
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        manualChunks: {
          // Split Cloudscape into its own chunk (largest dep)
          cloudscape: ["@cloudscape-design/components", "@cloudscape-design/global-styles"],
          // AWS Amplify in its own chunk
          aws: ["aws-amplify", "@aws-amplify/ui-react"],
          // React core
          react: ["react", "react-dom"],
        },
      },
    },
  },
});
