/**
 * ESLint flat config for Specivo's own JavaScript.
 *
 * Linted: the esbuild sources (js/) and the build script. Vendored
 * libraries (specivo/static/vendor), node_modules and the generated bundles
 * (specivo/static/dist) sit outside this directory or are ignored below, so
 * they are never linted.
 *
 * Existing violations are recorded in eslint-suppressions.json, so only new
 * code has to comply (the same ratchet as the noqa markers on the Python
 * side). See "Quality gates" in CONTRIBUTING.md.
 */
import js from '@eslint/js';
import globals from 'globals';

/** Libraries loaded from specivo/static/vendor/ with <script> tags. */
const vendorGlobals = {
    Alpine: 'readonly',
    htmx: 'readonly',
    EasyMDE: 'readonly',
    mermaid: 'readonly',
};

/**
 * Helpers that frontend/js/lib/globals.js attaches to `window`, so that
 * components and inline Alpine code in templates can call them by bare name.
 */
const specivoGlobals = {
    _getCsrfToken: 'readonly',
    spAnchorMenu: 'readonly',
    spBase: 'readonly',
    spBindAnchoredMenu: 'readonly',
    spFetch: 'readonly',
    spFormatDate: 'readonly',
    spLocale: 'readonly',
    spParseRepoAddress: 'readonly',
    spUrl: 'readonly',
};

/** Correctness rules on top of eslint:recommended. */
const correctnessRules = {
    'array-callback-return': 'error',
    'default-case-last': 'error',
    'no-constructor-return': 'error',
    'no-promise-executor-return': 'error',
    'no-self-compare': 'error',
    'no-template-curly-in-string': 'error',
    'no-unmodified-loop-condition': 'error',
    'no-unreachable-loop': 'error',
    'no-implied-eval': 'error',
    'no-new-func': 'error',
    'no-eval': 'error',
    // A leading underscore marks a binding as unused on purpose, as in
    // `catch (_e) {}`. Empty catch blocks are the codebase's way of saying
    // "best effort, ignore failure" (storage, clipboard, JSON parsing).
    'no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
    }],
    'no-empty': ['error', { allowEmptyCatch: true }],
    // Same ceiling as ruff's C90 (max-complexity) on the Python side.
    complexity: ['error', 15],
};

export default [
    {
        ignores: ['node_modules/', '**/*.min.js'],
    },
    {
        files: ['js/**/*.js'],
        ...js.configs.recommended,
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'module',
            globals: { ...globals.browser, ...vendorGlobals, ...specivoGlobals },
        },
        rules: { ...js.configs.recommended.rules, ...correctnessRules },
    },
    {
        files: ['build.js'],
        ...js.configs.recommended,
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'commonjs',
            globals: { ...globals.node },
        },
        rules: { ...js.configs.recommended.rules, ...correctnessRules },
    },
    {
        files: ['eslint.config.mjs'],
        ...js.configs.recommended,
        languageOptions: { sourceType: 'module', globals: { ...globals.node } },
    },
];
