import { defineRailway, github, preserve, project, service } from "railway/iac";

export default defineRailway(() => {
  const tessDesafio = service("tess-desafio", {
    source: github("rbfcdog/tess-desafio", { branch: "main", commitSha: "3816705616f948e47415c8ba9f9bd3fe448c69a0", rootDirectory: "backend", upstreamUrl: "https://github.com/rbfcdog/tess-desafio" }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/api/health",
    healthcheckTimeout: 120,
    replicas: { "sfo": 1 },
    env: { CONTEXT_INPUT_TOKENS: preserve(), CONTEXT_WINDOW_TOKENS: preserve(), ENCRYPTION_KEY: preserve(), GOOGLE_CLIENT_ID: preserve(), GOOGLE_CLIENT_SECRET: preserve(), GOOGLE_REDIRECT_URI: preserve(), MAX_MODEL_STEPS: preserve(), MAX_OUTPUT_TOKENS: preserve(), MAX_TOOL_CALLS: preserve(), MCP_TRUSTED_HOSTS: preserve(), OPENAI_API_KEY: preserve(), OPENAI_MODEL: preserve(), PUBLIC_APP_URL: preserve(), SUMMARY_OUTPUT_TOKENS: preserve(), SUPABASE_PUBLISHABLE_KEY: preserve(), SUPABASE_SECRET_KEY: preserve(), SUPABASE_URL: preserve() },
  });
  const frontend = service("frontend", {
    source: github("rbfcdog/tess-desafio", { branch: "main", checkSuites: false, rootDirectory: "frontend" }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/",
    healthcheckTimeout: 180,
    replicas: { "sfo": 1 },
    env: { BACKEND_URL: preserve(), NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY: preserve(), NEXT_PUBLIC_SUPABASE_URL: preserve(), RAILWAY_ROOT_DIRECTORY: preserve() },
  });

  return project("fortunate-nurturing", {
    variables: { managed: true },
    resources: [tessDesafio, frontend],
  });
});
