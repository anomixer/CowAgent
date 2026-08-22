import React, { useState } from 'react'
import apiClient from '../api/client'
import { t } from '../i18n'

interface LoginGateProps {
  // Multi-user backend: show username + password and a register toggle.
  // Legacy backend: password only (as before).
  multiuser?: boolean
  // Called once authentication succeeds, so the app can proceed to the main UI.
  onAuthenticated: () => void
}

const inputCls =
  'w-full px-4 py-2.5 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-[#1a1a1a] text-slate-800 dark:text-slate-100 text-sm outline-none focus:border-primary-500 transition-colors'
const buttonCls =
  'w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 bg-primary-500 hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg transition-colors text-sm font-medium cursor-pointer'

/**
 * Shown when the backend requires auth and the current session isn't
 * authenticated yet.
 *
 * - Legacy mode: a single access password.
 * - Multi-user mode: username + password, with a toggle to register (the first
 *   account becomes admin). On success the backend returns a bearer token,
 *   stored by the API client and echoed back via the Authorization header.
 */
const LoginGate: React.FC<LoginGateProps> = ({ multiuser = false, onAuthenticated }) => {
  const [mode, setMode] = useState<'login' | 'register'>(
    multiuser ? 'login' : 'login',
  )
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const isRegister = multiuser && mode === 'register'

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return
    if (isRegister) {
      if (password !== confirm) {
        setError(t('login_confirm_mismatch'))
        return
      }
    }
    setSubmitting(true)
    setError('')
    try {
      let res
      if (multiuser) {
        res = isRegister
          ? await apiClient.authRegister(username, password)
          : await apiClient.authLogin(password, username)
      } else {
        res = await apiClient.authLogin(password)
      }
      if (res.status === 'success') {
        onAuthenticated()
      } else {
        setError(res.message || t('login_error'))
      }
    } catch {
      setError(t('login_error'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="h-screen w-screen flex items-center justify-center bg-gray-50 dark:bg-[#111111]">
      <form onSubmit={submit} className="text-center space-y-6 max-w-md px-8 w-full">
        <img src="./logo.jpg" alt="CowAgent" className="w-16 h-16 rounded-2xl mx-auto shadow-lg shadow-primary-500/20" />
        <div className="space-y-2">
          <h1 className="text-xl font-bold text-slate-800 dark:text-slate-100">
            {t(isRegister ? 'register_title' : 'login_title')}
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            {t(isRegister ? 'register_desc' : 'login_desc')}
          </p>
        </div>

        {multiuser && (
          <input
            type="text"
            autoFocus
            value={username}
            onChange={(e) => {
              setUsername(e.target.value)
              if (error) setError('')
            }}
            placeholder={t('login_username')}
            className={inputCls}
            autoComplete="username"
          />
        )}
        <input
          type="password"
          autoFocus={!multiuser}
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
            if (error) setError('')
          }}
          placeholder={t('login_placeholder')}
          className={inputCls}
          autoComplete={isRegister ? 'new-password' : 'current-password'}
        />
        {isRegister && (
          <input
            type="password"
            value={confirm}
            onChange={(e) => {
              setConfirm(e.target.value)
              if (error) setError('')
            }}
            placeholder={t('login_confirm')}
            className={inputCls}
            autoComplete="new-password"
          />
        )}

        {error && <p className="text-sm text-red-500">{error}</p>}

        <button
          type="submit"
          disabled={
            submitting ||
            !password ||
            (multiuser && !username) ||
            (isRegister && !confirm)
          }
          className={buttonCls}
        >
          {submitting ? t('login_checking') : t(isRegister ? 'register_submit' : 'login_submit')}
        </button>

        {multiuser && (
          <button
            type="button"
            onClick={() => {
              setMode((m) => (m === 'login' ? 'register' : 'login'))
              setError('')
            }}
            className="text-sm text-primary-500 hover:text-primary-600 transition-colors cursor-pointer"
          >
            {t(isRegister ? 'login_switch_to_login' : 'login_switch_to_register')}
          </button>
        )}
      </form>
    </div>
  )
}

export default LoginGate
