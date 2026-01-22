"""纯逻辑版 Tetris 游戏内核（不依赖 bpy）。

这个模块只负责棋盘规则：
- 棋盘占用（width×height）
- 当前块（key/rotation/x/z）
- 碰撞检测、落地锁定、消行

渲染与交互（modal、点云、GN）全部在其它模块做。

为了给音效/事件提供信息，这里额外暴露：
- `last_locked`：最近一次 tick/drop 是否发生了落地锁定
- `last_cleared_lines`：最近一次锁定后消了几行

坐标约定：
- X：左右（列）
- Z：上下（行），向下落是 `dz=-1`
- Y 不参与（棋盘在 XZ 平面）
"""

from __future__ import annotations

from dataclasses import dataclass
import random

from .tetrominoes import TETROMINO_KEYS, cells_bbox, rotated_cells, srs_kicks


@dataclass
class ActivePiece:
    """当前正在下落的方块状态。"""

    key: str
    rotation: int
    x: int
    z: int
    system: str = "SRS"

    def cells_local(self) -> tuple[tuple[int, int], ...]:
        """返回该方块在本地坐标下的 4 个 cell（包含旋转）。

        Returns:
            本地坐标系下的 4 个格子坐标（cx, cz）。
        """

        return rotated_cells(self.key, self.rotation, system=self.system)

    def cells_global(self) -> tuple[tuple[int, int], ...]:
        """返回该方块在棋盘坐标系下的 4 个 cell（叠加 x/z 平移）。

        Returns:
            棋盘坐标系下的 4 个格子坐标（x, z）。
        """

        return tuple((self.x + cx, self.z + cz) for (cx, cz) in self.cells_local())


class TetrisGame:
    """Tetris 规则内核。

    外部主要通过这些方法驱动：
    - `spawn_piece()`：生成新块
    - `try_move(dx, dz)`：尝试移动
    - `try_rotate(cw)`：尝试旋转
    - `tick_down()`：一次重力 tick（能下移就下移，否则落地锁定/消行/生成下一块）
    - `hard_drop()`：硬降并立即锁定

    运行时的占用结构：
    - `occupied[(x,z)] = piece_key`（用于后续按 piece 着色/回放）
    """

    def __init__(
        self,
        *,
        width: int,
        height: int,
        seed: int | None = None,
        next_queue_size: int = 5,
        lines_per_level: int = 10,
        rotation_system: str = "SRS",
    ) -> None:
        """创建一个新的棋盘。

        Args:
            width: 棋盘宽（列数）。
            height: 棋盘高（行数）。
            seed: 随机种子（None 表示随机）。
            next_queue_size: Next 预览队列长度（用于 7-bag 生成器）。
            lines_per_level: 每升一级需要的累计消行数。
            rotation_system: 旋转系统标识（SRS 或 SIMPLE）。

        Raises:
            ValueError: width/height 非正。
            ValueError: next_queue_size 非正。
            ValueError: lines_per_level 非正。
        """

        if width <= 0 or height <= 0:
            raise ValueError("Board width/height must be > 0")

        self.width = int(width)
        self.height = int(height)
        self.rng = random.Random(seed)

        self.rotation_system = str(rotation_system).upper()

        self.next_queue_size = int(next_queue_size)
        if self.next_queue_size <= 0:
            raise ValueError("next_queue_size must be > 0")

        self.lines_per_level = int(lines_per_level)
        if self.lines_per_level <= 0:
            raise ValueError("lines_per_level must be > 0")

        # 计分/等级/统计
        self.score = 0
        self.level = 1
        self.lines_cleared_total = 0
        self.combo = -1
        self.back_to_back = False

        # 7-bag 生成器：bag + Next 队列（用于预览）。
        self._bag: list[str] = []
        self.next_queue: list[str] = []

        # Hold：保留块（None 表示还没 hold 过）。
        self.hold_key: str | None = None
        self.can_hold = True

        # 已落地占用：cell -> piece key
        self.occupied: dict[tuple[int, int], str] = {}

        # 当前块（可为空，例如 game over 或刚锁定后）
        self.current: ActivePiece | None = None

        self.game_over = False

        # 事件辅助字段：用于音效/录制判断
        self.last_locked = False
        self.last_cleared_lines = 0
        self.last_action = ""
        self.last_t_spin = False

        # 消行动画状态
        self.clearing_rows: list[int] = []
        self.clear_anim_progress: float = 0.0



    def reset(self) -> None:
        """重置游戏状态（相当于重新初始化，但保留 rng 实例）。"""
        self.score = 0
        self.level = 1
        self.lines_cleared_total = 0
        self.combo = -1
        self.back_to_back = False

        self._bag.clear()
        self.next_queue.clear()

        self.hold_key = None
        self.can_hold = True

        self.occupied.clear()
        self.current = None
        self.game_over = False

        self.last_locked = False
        self.last_cleared_lines = 0
        self.last_action = ""
        self.last_t_spin = False
        
        self.clearing_rows.clear()
        self.clear_anim_progress = 0.0


    def _bag_draw(self) -> str:
        """从 7-bag 生成器里抽取一个下一个方块 key。

        Returns:
            抽到的方块 key（I/O/T/S/Z/J/L）。
        """

        if not self._bag:
            self._bag = list(TETROMINO_KEYS)
            self.rng.shuffle(self._bag)

        return self._bag.pop()

    def _ensure_next_queue(self) -> None:
        """确保 next_queue 至少有 next_queue_size 个元素。"""

        while len(self.next_queue) < self.next_queue_size:
            self.next_queue.append(self._bag_draw())

    def set_next_queue_size(self, size: int) -> None:
        """调整 Next 队列长度。

        Args:
            size: 新的队列长度（>0）。

        Raises:
            ValueError: size 非正。
        """

        size = int(size)
        if size <= 0:
            raise ValueError("next_queue_size must be > 0")

        self.next_queue_size = size

        # 缩短队列时直接裁剪；增大队列则补齐。
        if len(self.next_queue) > size:
            self.next_queue = list(self.next_queue[:size])

        self._ensure_next_queue()

    def set_lines_per_level(self, value: int) -> None:
        """设置每升一级所需的累计消行数。

        Args:
            value: 每升一级所需的累计消行数（>0）。

        Raises:
            ValueError: value 非正。
        """

        value = int(value)
        if value <= 0:
            raise ValueError("lines_per_level must be > 0")

        self.lines_per_level = value

        # 根据当前累计消行重算 level，避免改参数后出现不一致。
        self.level = max(1, 1 + (int(self.lines_cleared_total) // int(self.lines_per_level)))

    def peek_next(self, *, count: int | None = None) -> tuple[str, ...]:
        """查看 Next 队列（不消费）。

        Args:
            count: 查看的数量；None 表示返回完整队列。

        Returns:
            Next 队列的 key 元组（最靠前的为下一块）。
        """

        self._ensure_next_queue()
        if count is None:
            return tuple(self.next_queue)
        return tuple(self.next_queue[: int(count)])

    def try_hold(self) -> bool:
        """执行一次 Hold（保留块）。

        规则：
        - 一次方块生命周期（从生成到落地锁定）内，只允许 Hold 一次
        - hold 为空：把当前块放入 hold，并从 next 队列生成新块
        - hold 非空：与 hold 交换，并生成被换出的块

        Returns:
            True 表示 hold 成功（并成功生成了新的 current）；False 表示 hold 被拒绝或 game over。
        """

        if self.current is None or self.game_over:
            return False

        if not bool(getattr(self, "can_hold", True)):
            return False

        current_key = self.current.key

        if self.hold_key is None:
            self.hold_key = current_key
            self.current = None
            ok = self.spawn_piece()
        else:
            swap_key = self.hold_key
            self.hold_key = current_key
            self.current = None
            ok = self.spawn_piece(key=swap_key)

        # 本回合 Hold 已使用，直到下一次 lock/spawn 才能再 Hold。
        self.can_hold = False
        return bool(ok)

    def _collides_cells(self, cells: tuple[tuple[int, int], ...]) -> bool:
        """检测一组 cell 是否与棋盘边界/占用发生碰撞。

        Args:
            cells: 待检查的棋盘坐标 cell 列表。

        Returns:
            如果发生碰撞（越界或与占用重叠）返回 True，否则 False。
        """

        for (x, z) in cells:
            # 左右边界
            if x < 0 or x >= self.width:
                return True

            # 底部边界（z<0 表示落出棋盘）
            if z < 0:
                return True

            # 与已有占用碰撞（只检查棋盘内）
            if z < self.height and (x, z) in self.occupied:
                return True

        return False

    def _spawn_position_for_key(self, key: str) -> tuple[int, int]:
        """为某个 key 计算一个默认出生位置（尽量居中且不越界）。

        Args:
            key: 方块 key（I/O/T/S/Z/J/L）。

        Returns:
            (spawn_x, spawn_z)
        """

        local = rotated_cells(key, 0)
        _, _, max_x, max_z = cells_bbox(local)

        # 默认居中偏左一点（简化版，可后续改成标准 spawn）
        spawn_x = (self.width // 2) - 2

        # 防止超出右边界：保证 max_x 仍在棋盘内
        spawn_x = max(0, min(spawn_x, self.width - 1 - max_x))

        # 出生高度放在最顶端（让方块完全进入棋盘）
        spawn_z = (self.height - 1) - max_z

        return spawn_x, spawn_z

    def spawn_piece(self, *, key: str | None = None) -> bool:
        """生成一个新的当前块。

        Args:
            key: 指定形状；如果不指定则从 Next 队列（7-bag）取下一块。

        Returns:
            生成成功返回 True；如果出生即碰撞则 game over 并返回 False。
        """

        if self.game_over:
            return False

        if key is None:
            self._ensure_next_queue()
            key = self.next_queue.pop(0)
            self._ensure_next_queue()

        x, z = self._spawn_position_for_key(key)
        piece = ActivePiece(key=key, rotation=0, x=x, z=z, system=self.rotation_system)

        # 出生位置直接碰撞：视为 game over
        if self._collides_cells(piece.cells_global()):
            self.current = None
            self.game_over = True
            return False

        self.current = piece

        # 新块生成后，允许下一次 Hold。
        self.can_hold = True

        return True

    def try_move(self, *, dx: int, dz: int) -> bool:
        """尝试移动当前块。

        Args:
            dx: 左右移动。
            dz: 上下移动（下落是 -1）。

        Returns:
            True 表示移动成功；False 表示被碰撞阻止。
        """

        if self.current is None or self.game_over:
            return False

        moved = ActivePiece(
            key=self.current.key,
            rotation=self.current.rotation,
            x=self.current.x + int(dx),
            z=self.current.z + int(dz),
            system=self.current.system,
        )

        if self._collides_cells(moved.cells_global()):
            return False

        self.current = moved
        self.last_action = "MOVE"
        return True

    def can_move(self, *, dx: int, dz: int) -> bool:
        """测试当前块是否能移动到目标位置（不改变状态）。

        Args:
            dx: 左右偏移。
            dz: 上下偏移（下落为 -1）。

        Returns:
            True 表示不会碰撞（可移动）；False 表示会碰撞或无 current。
        """

        if self.current is None or self.game_over:
            return False

        candidate = ActivePiece(
            key=self.current.key,
            rotation=self.current.rotation,
            x=self.current.x + int(dx),
            z=self.current.z + int(dz),
            system=self.current.system,
        )

        return not self._collides_cells(candidate.cells_global())

    def try_rotate(self, *, cw: bool) -> bool:
        """尝试旋转当前块。

        支持两种系统：
        - SRS: 按 Guideline SRS 表依次尝试 5 个平移偏移（Kick）。
        - SIMPLE: 执行旋转+归一化，并尝试简单的左右平移 (0, 1, -1, 2, -2) 避开障碍。

        Args:
            cw: True 表示顺时针；False 表示逆时针。

        Returns:
            True 表示旋转成功；False 表示所有尝试都失败。
        """

        if self.current is None or self.game_over:
            return False

        from_rotation = int(self.current.rotation) % 4
        delta = 1 if cw else -1
        to_rotation = (from_rotation + delta) % 4

        # 核心逻辑切换
        if self.rotation_system == "SIMPLE":
            # 经典简单旋转：仅测试简单的左右平移。
            kicks = [(0, 0), (1, 0), (-1, 0), (2, 0), (-2, 0)]
        else:
            # 标准 SRS：查表获取针对该形状与旋转转换的 5 个 (dx, dz) 偏移。
            kicks = srs_kicks(key=self.current.key, from_rotation=from_rotation, to_rotation=to_rotation)

        for dx, dz in kicks:
            candidate = ActivePiece(
                key=self.current.key,
                rotation=to_rotation,
                x=self.current.x + int(dx),
                z=self.current.z + int(dz),
                system=self.rotation_system,
            )
            if not self._collides_cells(candidate.cells_global()):
                self.current = candidate
                self.last_action = "ROTATE"
                return True

        return False

    def lock_current(self) -> None:
        """将当前块落地锁定，并在需要时生成下一块。

        该方法不会尝试下移；只执行“锁定/消行/生成下一块”这一步，
        便于上层实现 Lock Delay 等手感逻辑。
        """

        if self.current is None or self.game_over:
            return

        self._lock_current()

        # 锁定后，如果没 game over，则生成下一块。
        if not self.game_over and self.current is None:
            self.spawn_piece()

    def _lock_current(self) -> None:
        """把当前块锁定到 occupied，并执行消行。

        注意：这里会清空 `current`。
        """

        if self.current is None:
            return

        self.last_locked = True
        self.last_cleared_lines = 0

        piece = self.current

        cells = piece.cells_global()
        for (x, z) in cells:
            # 超出顶部表示溢出：game over（仍然尝试写入棋盘内部分）
            if z >= self.height:
                self.game_over = True
                continue

            self.occupied[(x, z)] = piece.key

        self.last_t_spin = self._detect_t_spin(piece=piece)
        self.current = None

        # 检查是否有满行
        counts = [0] * self.height
        for (x, z) in self.occupied:
            if 0 <= z < self.height:
                counts[z] += 1
        
        full_rows = [z for z, c in enumerate(counts) if c == self.width]
        
        if full_rows:
            self.clearing_rows = full_rows
            self.clear_anim_progress = 0.0
            self.last_cleared_lines = len(full_rows)
            # 立即计分，但延迟消行（动画后执行）
            self._apply_line_clear_scoring(cleared=int(self.last_cleared_lines))
        else:
            self.clearing_rows = []
            self.clear_anim_progress = 0.0
            self._apply_line_clear_scoring(cleared=0)

    def finalize_clear(self) -> None:
        """动画结束后，真正执行消行并将上方块下移。"""
        if not self.clearing_rows:
            return

        full_set = set(self.clearing_rows)

        def rows_below(z: int) -> int:
            return sum(1 for r in self.clearing_rows if r < z)

        # 重建 occupied：删掉满行，并把上方下移
        new_occupied: dict[tuple[int, int], str] = {}
        for (x, z), key in self.occupied.items():
            if z in full_set:
                continue

            shift = rows_below(z)
            new_occupied[(x, z - shift)] = key

        self.occupied = new_occupied
        self.clearing_rows = []
        self.clear_anim_progress = 0.0

    def _clear_lines(self) -> int:
        """消行并将上方块下移。

        Returns:
            消除的行数。
        """

        counts = [0] * self.height

        # 统计每一行的占用数量
        for (x, z) in self.occupied:
            if 0 <= z < self.height:
                counts[z] += 1

        full_rows = [z for z, c in enumerate(counts) if c == self.width]
        if not full_rows:
            return 0

        full_set = set(full_rows)

        def rows_below(z: int) -> int:
            """返回在 z 下方被消除的行数（用于计算下移量）。

            Args:
                z: 当前行号。

            Returns:
                小于 z 的满行数量。
            """

            return sum(1 for r in full_rows if r < z)

        # 重建 occupied：删掉满行，并把上方下移
        new_occupied: dict[tuple[int, int], str] = {}
        for (x, z), key in self.occupied.items():
            if z in full_set:
                continue

            shift = rows_below(z)
            new_occupied[(x, z - shift)] = key

        self.occupied = new_occupied
        return len(full_rows)

    def _detect_t_spin(self, *, piece: ActivePiece) -> bool:
        """粗略 T-Spin 检测（满足大部分 Guideline 判定的必要条件）。

        判定：
        - 当前锁定块是 T
        - 最近一次有效操作是旋转（ROTATE）
        - 以 T 的旋转中心为基准，4 个角落中至少 3 个被占用或越界
        """

        if str(getattr(piece, "key", "")).upper() != "T":
            return False

        if str(getattr(self, "last_action", "")).upper() != "ROTATE":
            return False

        # T/S/Z/J/L 的旋转中心在 3x3 包围盒的 (1,1)。
        px = int(getattr(piece, "x", 0)) + 1
        pz = int(getattr(piece, "z", 0)) + 1

        corners = (
            (px - 1, pz - 1),
            (px + 1, pz - 1),
            (px - 1, pz + 1),
            (px + 1, pz + 1),
        )

        occ = set(self.occupied.keys())
        blocked = 0
        for (x, z) in corners:
            if x < 0 or x >= int(self.width) or z < 0 or z >= int(self.height):
                blocked += 1
            elif (x, z) in occ:
                blocked += 1

        return blocked >= 3

    def _apply_line_clear_scoring(self, *, cleared: int) -> None:
        """根据本次消行数更新计分/等级/连击状态。

        Args:
            cleared: 本次锁定后消除的行数（0..4）。
        """

        cleared = int(cleared)

        if cleared <= 0:
            # 没有消行：连击与 B2B 断开。
            self.combo = -1
            self.back_to_back = False
            return

        self.lines_cleared_total += cleared

        if int(getattr(self, "combo", -1)) >= 0:
            self.combo = int(self.combo) + 1
        else:
            self.combo = 0

        level = int(getattr(self, "level", 1) or 1)

        t_spin = bool(getattr(self, "last_t_spin", False))
        scoring_mode = str(getattr(self, "scoring_mode", "GUIDELINE") or "GUIDELINE").upper()

        base = 0
        if scoring_mode == "EXPONENTIAL":
            base = 100 * (2 ** max(0, cleared - 1))
        else:
            if t_spin:
                base = {1: 800, 2: 1200, 3: 1600}.get(cleared, 0)
            else:
                base = {1: 100, 2: 300, 3: 500, 4: 800}.get(cleared, 0)

        points = int(base) * level

        b2b_eligible = bool(t_spin) or cleared == 4
        if b2b_eligible and bool(getattr(self, "back_to_back", False)):
            points = int(points * 1.5)

        if int(self.combo) > 0:
            combo_mode = str(getattr(self, "combo_scoring_mode", "MULTIPLY") or "MULTIPLY").upper()
            if combo_mode == "ADD":
                points += 50 * int(self.combo) * level
            else:
                step = float(getattr(self, "combo_multiplier_step", 0.25) or 0.25)
                step = max(0.0, step)
                points = int(points * (1.0 + step * int(self.combo)))

        self.score += int(points)
        self.back_to_back = bool(b2b_eligible)

        # level 更新：按累计消行推进。
        self.level = max(1, 1 + (int(self.lines_cleared_total) // int(self.lines_per_level)))

    def get_ghost(self) -> ActivePiece | None:
        """计算当前块的 Ghost（阴影）位置（即硬降落点）。

        Returns:
            Ghost 状态的 ActivePiece；如果当前无块则返回 None。
        """

        if self.current is None or self.game_over:
            return None

        # 模拟硬降：从当前位置向下探测，直到碰撞
        # 注意：这里只计算位置，不改变游戏状态
        ghost = ActivePiece(
            key=self.current.key,
            rotation=self.current.rotation,
            x=self.current.x,
            z=self.current.z,
            system=self.current.system,
        )

        while True:
            # 尝试向下移一格
            candidate = ActivePiece(
                key=ghost.key,
                rotation=ghost.rotation,
                x=ghost.x,
                z=ghost.z - 1,
                system=ghost.system,
            )
            # 如果碰撞，则当前 ghost 位置就是最终位置
            if self._collides_cells(candidate.cells_global()):
                break
            ghost = candidate

        return ghost

    def tick_down(self) -> bool:
        """重力 tick：能下移则下移，否则锁定并生成下一块。

        Returns:
            True 表示当前块成功下移；False 表示发生了锁定（或无法移动）。

        Side effects:
            - 可能会触发 `last_locked=True` 并更新 `last_cleared_lines`
            - 可能会把 current 清空并生成新块
        """

        if self.current is None:
            return False

        # 每次 tick 都先清空事件字段，避免沿用上一次结果
        self.last_locked = False
        self.last_cleared_lines = 0

        if self.try_move(dx=0, dz=-1):
            return True

        # 下移失败：锁定当前块
        self._lock_current()

        # 锁定后，如果没 game over，生成下一块
        if not self.game_over and self.current is None:
            self.spawn_piece()

        return False

    def hard_drop(self) -> int:
        """硬降：尽可能下移到无法下移，然后锁定。

        Returns:
            本次硬降下移的格子数（用于 HUD/调试）。

        Side effects:
            - 会设置 last_locked/last_cleared_lines
            - 会累计硬降得分（每格 +2）
            - 可能 game over
        """

        if self.current is None:
            return 0

        self.last_locked = False
        self.last_cleared_lines = 0

        distance = 0

        # 关键分支：一直下移直到碰撞
        while self.try_move(dx=0, dz=-1):
            distance += 1

        self.last_action = "DROP"

        # 硬降得分：每格 +2（常见 Guideline 规则）。
        if distance > 0:
            self.score += int(distance) * 2

        self._lock_current()

        if not self.game_over and self.current is None:
            self.spawn_piece()

        return int(distance)
