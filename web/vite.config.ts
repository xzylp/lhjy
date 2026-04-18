import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: '/dashboard/',
  build: {
    outDir: '../src/ashare_system/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/system': 'http://127.0.0.1:18793',
      '/runtime': 'http://127.0.0.1:18793',
    }
  }
})
