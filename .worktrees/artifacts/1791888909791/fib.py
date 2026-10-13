def fib(n):
    """返回前 n 项斐波那契数列（迭代实现）。"""
    seq = []
    a, b = 0, 1
    for _ in range(n):
        seq.append(a)
        a, b = b, a + b
    return seq


def fib_memo(n, _cache={0: 0, 1: 1}):
    """返回第 n 个斐波那契数（带记忆化递归）。"""
    if n in _cache:
        return _cache[n]
    _cache[n] = fib_memo(n - 1) + fib_memo(n - 2)
    return _cache[n]


if __name__ == "__main__":
    n = 10
    print(f"前 {n} 项: {fib(n)}")
    print(f"第 {n} 项: {fib_memo(n)}")
    print(f"第 50 项: {fib_memo(50)}")
