import { Link } from 'react-router-dom'

export default function NotFoundPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3">
      <p className="font-mono text-sm text-fg-faint">404</p>
      <h1 className="text-lg font-medium">这个页面不存在</h1>
      <Link to="/" className="text-sm text-brand hover:underline">
        回到首页
      </Link>
    </div>
  )
}
