// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useAuthenticator } from "@aws-amplify/ui-react";

export function useAuth() {
  const { user, signOut, authStatus } = useAuthenticator((ctx) => [ctx.user, ctx.authStatus]);

  return {
    isAuthenticated: authStatus === "authenticated",
    user,
    email: user?.signInDetails?.loginId ?? user?.username ?? "",
    signOut,
    isLoading: authStatus === "configuring",
  };
}
