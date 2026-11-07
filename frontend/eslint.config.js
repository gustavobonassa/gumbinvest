// Flat config. Type-aware rules are left out on purpose: `tsc --noEmit`
// (npm run typecheck) already owns type correctness, and the type-checked
// presets would double the lint time for little that tsc does not catch.
// Formatting belongs to Prettier — eslint-config-prettier switches off every
// stylistic rule that would fight it.
import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "node_modules"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Dev-only concern (hot reload drops component state). The names below
      // live beside the component they belong to on purpose — the toast hook
      // with its provider, a chart's data shaper with the chart — and moving
      // them would churn two dozen imports for nothing at runtime.
      "react-refresh/only-export-components": [
        "warn",
        {
          allowConstantExport: true,
          allowExportNames: [
            "useToast",
            "describeError",
            "kindSlices",
            "GOAL_OPTIONS",
            "HORIZON_OPTIONS",
            "RISK_OPTIONS",
          ],
        },
      ],
      // `_`-prefixed names are the conventional "deliberately unused".
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["**/*.test.{ts,tsx}", "src/test/**"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
  prettier,
);
