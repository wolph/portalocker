LK_LOCK: int
LK_NBLCK: int
LK_NBRLCK: int
LK_RLCK: int
LK_UNLCK: int

def locking(file: int, mode: int, lock_length: int) -> int: ...
