// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import ReactDOM from "react-dom/client";
import "@cloudscape-design/global-styles/index.css";
import { applyMode, Mode } from "@cloudscape-design/global-styles";
import { AuthProvider } from "./components/auth/AuthProvider";
import App from "./App";

applyMode(Mode.Dark);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </React.StrictMode>,
);
