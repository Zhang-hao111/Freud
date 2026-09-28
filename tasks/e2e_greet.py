"""E2E 验证任务：实现 greet 函数。"""


def greet(name):
    """返回问候字符串。

    参数:
        name: 待问候的名字（任意可转为字符串的对象）。

    返回:
        字符串 "你好, <name>"。
    """
    return "你好, " + str(name)


if __name__ == "__main__":
    print(greet("世界"))
