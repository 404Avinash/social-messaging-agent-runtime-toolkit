// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    // Keeps `vitest run` green if a filtered run matches no files, so CI fails
    // on real breakage rather than on an empty selection.
    passWithNoTests: true,
  },
});
