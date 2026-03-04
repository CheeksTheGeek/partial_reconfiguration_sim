"""Port-index-based Python API generator.

Generates Python API classes that use read_port(idx)/write_port(idx, value)
on SharedMemoryInterface. No numpy.
"""
from dataclasses import dataclass
from typing import List


@dataclass
class PortSpec:
    """Specification for a single port in the API."""
    name: str
    width: int
    direction: str  # 'input'/'output' (static) or 'to_rm'/'from_rm' (partition)
    index: int


class ApiGenerator:
    """Generates Python API classes for port-index-based access."""

    def _is_writable(self, direction: str) -> bool:
        return direction in ('input', 'to_rm')

    def _class_name_from(self, name: str) -> str:
        return ''.join(w.capitalize() for w in name.split('_')) + 'API'

    def generate_api_code(self, class_name: str, module_name: str, ports: List[PortSpec]) -> str:
        """Generate Python source code for an API class."""
        lines = [
            f'class {class_name}:',
            f'    """Auto-generated API for {module_name}."""',
            '',
            '    def __init__(self, shm):',
            '        self._shm = shm',
            '',
        ]

        for port in ports:
            nc = (port.width + 63) // 64  # num_chunks
            if self._is_writable(port.direction):
                if nc > 1:
                    lines.append(f'    def write_{port.name}(self, value: int):')
                    lines.append(f'        for _i in range({nc}):')
                    lines.append(f'            self._shm.write_port({port.index} + _i, (value >> (_i * 64)) & 0xFFFFFFFFFFFFFFFF)')
                    lines.append('')
                    lines.append(f'    def read_{port.name}(self) -> int:')
                    lines.append(f'        _result = 0')
                    lines.append(f'        for _i in range({nc}):')
                    lines.append(f'            _result |= self._shm.read_port({port.index} + _i) << (_i * 64)')
                    lines.append(f'        return _result')
                    lines.append('')
                else:
                    lines.append(f'    def write_{port.name}(self, value: int):')
                    lines.append(f'        self._shm.write_port({port.index}, value)')
                    lines.append('')
                    lines.append(f'    def read_{port.name}(self) -> int:')
                    lines.append(f'        return self._shm.read_port({port.index})')
                    lines.append('')
            else:
                if nc > 1:
                    lines.append(f'    def read_{port.name}(self) -> int:')
                    lines.append(f'        _result = 0')
                    lines.append(f'        for _i in range({nc}):')
                    lines.append(f'            _result |= self._shm.read_port({port.index} + _i) << (_i * 64)')
                    lines.append(f'        return _result')
                    lines.append('')
                else:
                    lines.append(f'    def read_{port.name}(self) -> int:')
                    lines.append(f'        return self._shm.read_port({port.index})')
                    lines.append('')

        return '\n'.join(lines)

    def generate_api_class(self, class_name: str, module_name: str, ports: List[PortSpec]) -> type:
        """Generate and return a live Python class object."""
        code = self.generate_api_code(class_name, module_name, ports)
        namespace = {}
        exec(code, namespace)  # noqa: S102
        return namespace[class_name]
