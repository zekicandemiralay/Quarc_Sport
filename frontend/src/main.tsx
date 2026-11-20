import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import RequiredUpdateGate from './components/RequiredUpdateGate'
import StorePromo from './components/StorePromo'
import './i18n'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
    <RequiredUpdateGate packageName="com.quarc.sport" />
    <StorePromo dialog />
  </React.StrictMode>
)
