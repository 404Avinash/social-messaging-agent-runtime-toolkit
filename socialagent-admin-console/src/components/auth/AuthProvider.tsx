// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState, type PropsWithChildren } from "react";
import { Authenticator, useAuthenticator } from "@aws-amplify/ui-react";
import { Amplify } from "aws-amplify";
import { fetchAuthSession } from "aws-amplify/auth";
import "@aws-amplify/ui-react/styles.css";
import awsExports from "@/aws-exports";
import { setAuthTokenProvider } from "@/api/client";
import { GroupsContext, groupsFromClaim } from "@/hooks/use-groups";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";

Amplify.configure(awsExports);

type Session = Awaited<ReturnType<typeof fetchAuthSession>>;

function TokenSync({ children }: PropsWithChildren) {
  const { authStatus } = useAuthenticator((ctx) => [ctx.authStatus]);
  const [tokenReady, setTokenReady] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [groups, setGroups] = useState<string[]>([]);

  useEffect(() => {
    if (authStatus !== "authenticated") {
      setAuthTokenProvider(async () => null);
      setTokenReady(false);
      setAuthError(null);
      setGroups([]);
      return;
    }

    // The groups ride in the same ID token, so read them on every resolution
    // and a refreshed token carries a group change into the UI. Keeping the old
    // array when nothing changed saves a re-render per request.
    const syncGroups = (session: Session) => {
      const next = groupsFromClaim(session.tokens?.idToken?.payload["cognito:groups"]);
      setGroups((prev) => (prev.join() === next.join() ? prev : next));
    };

    // Hand the client a resolver rather than a token. The ID token lasts an hour
    // and this effect only re-runs on an authStatus change, so a captured token
    // went stale and every request 401'd — while Amplify still reported
    // `authenticated` off its 24h refresh token, so the Authenticator never
    // re-prompted and the console stayed broken until a manual reload.
    // fetchAuthSession refreshes on demand, so per-request resolution stays valid.
    setAuthTokenProvider(async () => {
      const session = await fetchAuthSession();
      syncGroups(session);
      return session.tokens?.idToken?.toString() ?? null;
    });

    // Probe once so a session that cannot be established surfaces here instead of
    // as a failure on every page. Without a catch, a rejection left tokenReady
    // false forever: a permanent "Authenticating..." plus an unhandled rejection.
    //
    // The probe reads the cached session and does not force a refresh: a failed
    // refresh other than a network error makes the Authenticator sign the user
    // out, so forcing one on every page load could end a valid session. The
    // groups claim then lags a group change until Amplify refreshes the token
    // (within the hour) or the user signs in again; the backend decides anyway.
    let cancelled = false;
    fetchAuthSession()
      .then((session) => {
        if (!cancelled) {
          syncGroups(session);
          setAuthError(null);
          setTokenReady(true);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setAuthError(e instanceof Error ? e.message : String(e));
          setTokenReady(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authStatus]);

  if (authError) {
    return (
      <Box padding={{ top: "xxxl" }}>
        <Alert type="error" header="Could not establish your session">
          {authError}. Reload the page to try again, or sign out and back in.
        </Alert>
      </Box>
    );
  }

  if (!tokenReady) {
    return <Box textAlign="center" padding={{ top: "xxxl" }}>Authenticating...</Box>;
  }

  return <GroupsContext.Provider value={groups}>{children}</GroupsContext.Provider>;
}

export function AuthProvider({ children }: PropsWithChildren) {
  return (
    <Authenticator.Provider>
      <Authenticator hideSignUp variation="modal">
        <TokenSync>{children}</TokenSync>
      </Authenticator>
    </Authenticator.Provider>
  );
}
