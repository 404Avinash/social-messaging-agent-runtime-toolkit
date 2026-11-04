// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Values injected at build time by CodeBuild via VITE_* env vars.
// For local dev, create .env.local with these values.

const {
  VITE_USER_POOL_ID,
  VITE_USER_POOL_CLIENT_ID,
  VITE_IDENTITY_POOL_ID,
  VITE_AWS_REGION,
} = import.meta.env;

const awsExports = {
  aws_project_region: VITE_AWS_REGION,
  aws_cognito_identity_pool_id: VITE_IDENTITY_POOL_ID,
  aws_cognito_region: VITE_AWS_REGION,
  aws_user_pools_id: VITE_USER_POOL_ID,
  aws_user_pools_web_client_id: VITE_USER_POOL_CLIENT_ID,
  oauth: {},
  aws_cognito_login_mechanisms: ["EMAIL"],
  aws_cognito_signup_attributes: ["EMAIL"],
  aws_cognito_mfa_configuration: "ON",
  aws_cognito_mfa_types: ["TOTP"],
  aws_cognito_password_protection_settings: {
    passwordPolicyMinLength: 12,
    passwordPolicyCharacters: [
      "REQUIRES_LOWERCASE",
      "REQUIRES_UPPERCASE",
      "REQUIRES_NUMBERS",
      "REQUIRES_SYMBOLS",
    ],
  },
  aws_cognito_verification_mechanisms: ["EMAIL"],
};

export default awsExports;
